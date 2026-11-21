# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Reversible PulseAudio-compatible PipeWire session operations, never global config."""
import copy
import json
import os
import re
import shlex
import time
from pathlib import Path
from uuid import uuid4

from app.core import ControlError, read_json, run, save_json, state_dir

NAME = re.compile(r'^[A-Za-z0-9_.:-]+$')


def arguments(module):
    try:
        return dict(token.split('=', 1) for token in shlex.split(module.get('argument', '')) if '=' in token)
    except ValueError as exc:
        raise ControlError('Argumentos de módulo AEC no válidos.') from exc


class PipeWire:
    def __init__(self, runner=run, journal=None):
        self.runner = runner
        self.journal = Path(journal or state_dir() / 'audio-session.json')

    def command(self, *args):
        return self.runner(['pactl', *map(str, args)])

    def items(self, kind):
        data = json.loads(self.command('--format=json', 'list', kind))
        # Ubuntu's pactl 17 omits module indices from JSON. Resolve them from
        # the live short inventory; never assume an index or rely on list order.
        if kind == 'modules' and any(m['name'] == 'module-echo-cancel' and 'index' not in m for m in data):
            indices = {}
            for line in self.command('list', 'short', 'modules').splitlines():
                parts = line.split('\t')
                if len(parts) >= 3 and parts[0].isdigit() and parts[1] == 'module-echo-cancel':
                    indices.setdefault((parts[1], parts[2]), []).append(int(parts[0]))
            for module in data:
                if module['name'] == 'module-echo-cancel' and 'index' not in module:
                    candidates = indices.get((module['name'], module.get('argument', '')), [])
                    if not candidates:
                        raise ControlError('El inventario AEC cambió durante la lectura; actualiza.')
                    module['index'] = candidates.pop(0)
        return data

    def snapshot(self):
        data = {kind: self.items(kind) for kind in ('sources', 'sinks', 'modules', 'source-outputs', 'sink-inputs')}
        data['default_source'] = self.command('get-default-source')
        data['default_sink'] = self.command('get-default-sink')
        return data

    @staticmethod
    def echo_modules(data):
        return [m for m in data['modules'] if m['name'] == 'module-echo-cancel']

    @staticmethod
    def physical(data, kind):
        return [d for d in data[kind] if d['name'].startswith('alsa_') and
                not d['name'].endswith('.monitor')]

    def validate_pair(self, source, sink, data=None):
        data = data or self.snapshot()
        for name, kind in ((source, 'sources'), (sink, 'sinks')):
            if not NAME.fullmatch(name or '') or name not in [d['name'] for d in self.physical(data, kind)]:
                raise ControlError(f'Dispositivo físico no disponible: {name}')
        return data

    def set_volume(self, kind, name, volume):
        if kind not in ('source', 'sink') or type(volume) not in (int, float) or not 0 <= volume <= 100:
            raise ControlError('Volumen no válido (0–100 %).')
        data = self.snapshot()
        if name not in [d['name'] for d in self.physical(data, kind + 's')]:
            raise ControlError('El dispositivo de volumen ya no está disponible.')
        device = next(d for d in data[kind + 's'] if d['name'] == name)
        record = self._remember(data)
        volumes = record.setdefault('volumes', {})
        entry = volumes.setdefault(kind + ':' + name, {
            'kind': kind, 'name': name,
            'before': [v['value'] for v in device['volume'].values()]})
        save_json(self.journal, record)
        self.command('set-' + kind + '-volume', name, f'{volume}%')
        device = next(d for d in self.items(kind + 's') if d['name'] == name)
        entry['expected'] = [v['value'] for v in device['volume'].values()]
        save_json(self.journal, record)

    def _restore_volumes(self, record, data):
        for entry in record.get('volumes', {}).values():
            device = next((d for d in data[entry['kind'] + 's'] if d['name'] == entry['name']), None)
            if device is None:
                raise ControlError('No está conectado el dispositivo cuyo volumen se va a restaurar.')
            current = [v['value'] for v in device['volume'].values()]
            if current != entry.get('expected', entry['before']):
                raise ControlError('El volumen cambió fuera de la aplicación. No se sobrescribirá.')
            self.command('set-' + entry['kind'] + '-volume', entry['name'], *entry['before'])
            entry['expected'] = entry['before']
            save_json(self.journal, record)

    def _defaults(self, source, sink):
        before_source = self.command('get-default-source')
        self.command('set-default-source', source)
        try:
            self.command('set-default-sink', sink)
        except ControlError:
            self.command('set-default-source', before_source)
            raise
        if self.command('get-default-source') != source or self.command('get-default-sink') != sink:
            raise ControlError('PipeWire no confirmó los dispositivos predeterminados.')

    def _load(self, argument):
        index = int(self.command('load-module', 'module-echo-cancel', *shlex.split(argument)))
        for _ in range(15):
            modules = self.items('modules')
            found = next((m for m in modules if m['name'] == 'module-echo-cancel' and m.get('index') == index), None)
            if found:
                return found
            time.sleep(0.1)
        raise ControlError('No se pudo verificar el módulo AEC creado.')

    @staticmethod
    def _same(a, b):
        return a.get('name') == b.get('name') and a.get('argument') == b.get('argument')

    @staticmethod
    def _new_record(data):
        return {'version': 1, 'before': {
            'default_source': data['default_source'], 'default_sink': data['default_sink'],
            'modules': copy.deepcopy(PipeWire.echo_modules(data))}, 'active': None, 'moves': [],
            'expected_defaults': [data['default_source'], data['default_sink']]}

    def _remember(self, data):
        record = read_json(self.journal)
        if record is None:
            record = self._new_record(data)
            save_json(self.journal, record)
        return record

    @staticmethod
    def _reconciliation_view(data):
        # Compare identities as well as names: hotplug can reuse a device name.
        return ([data['default_source'], data['default_sink']],
                sorted((d['name'], str(d.get('index'))) for d in data['sources']),
                sorted((d['name'], str(d.get('index'))) for d in data['sinks']),
                sorted((m.get('argument', ''), str(m.get('index')))
                       for m in PipeWire.echo_modules(data)))

    @staticmethod
    def _live_moves(moves, data):
        retained = []
        for move in moves:
            devices = data['sources' if move['field'] == 'source' else 'sinks']
            names = {device['name']: device['index'] for device in devices}
            stream = next((item for item in data[move['kind']]
                           if item['index'] == move['index'] and item.get('client') == move['client']), None)
            if (move['from'] in names and move['to'] in names and stream
                    and stream.get(move['field']) == names[move['to']]):
                retained.append(copy.deepcopy(move))
        return retained

    def reconcile(self, data=None):
        """Retire obsolete routing claims, without issuing any audio writes.

        A deliberate disable with matching physical defaults retains its original
        undo session. Missing AEC plus obsolete defaults/lost ownership is different:
        archive that session and use the verified live physical routes as baseline.
        Independent volume and still-valid stream undo entries keep their guards.
        """
        try:
            original = self.journal.read_bytes()
        except FileNotFoundError:
            return {'state': 'none'}
        try:
            record = json.loads(original)
            if (not isinstance(record, dict) or record.get('version') != 1 or
                    not isinstance(record.get('expected_defaults'), list) or
                    len(record['expected_defaults']) != 2 or
                    not isinstance(record.get('before'), dict) or
                    not isinstance(record['before'].get('modules'), list)):
                raise ValueError('invalid journal')
        except (ValueError, TypeError) as exc:
            raise ControlError('Diario de audio no válido; se conserva sin modificar.') from exc
        data = data or self.snapshot()
        defaults = [data['default_source'], data['default_sink']]
        matches = record['expected_defaults'] == defaults
        modules = self.echo_modules(data)
        if modules:
            owned_missing = bool(record.get('active') or record.get('pending_argument')) and not self._active(record, data)
            return {'state': 'current' if matches and len(modules) == 1 and not owned_missing else 'conflict'}
        lost_active = bool(record.get('active') or record.get('pending_argument'))
        # Do not assume ownership of orphaned virtual devices or foreign routes.
        virtual_names = {'scl_mic_aec', 'scl_speakers_aec'}
        historical = list(record['before']['modules'])
        if record.get('active'):
            historical.append(record['active'])
        if record.get('pending_argument'):
            historical.append({'argument': record['pending_argument']})
        for module in historical:
            args = arguments(module)
            virtual_names.update(value for key, value in args.items() if key in ('source_name', 'sink_name'))
        names = {d['name'] for kind in ('sources', 'sinks') for d in data[kind]}
        if virtual_names & names:
            return {'state': 'conflict', 'reason': 'virtual_nodes_present'}
        for kind, default in (('sources', defaults[0]), ('sinks', defaults[1])):
            if default not in [d['name'] for d in self.physical(data, kind)]:
                return {'state': 'conflict', 'reason': 'defaults_not_live_physical'}
        if matches and not lost_active:
            return {'state': 'current'}
        # A stale caller snapshot must never authorize rebasing a newer session.
        fresh = self.snapshot()
        if self._reconciliation_view(data) != self._reconciliation_view(fresh):
            return {'state': 'conflict', 'reason': 'audio_changed_during_reconciliation'}
        replacement = self._new_record(fresh)
        if record.get('volumes'):
            replacement['volumes'] = copy.deepcopy(record['volumes'])
        for key in ('moves', 'evacuated'):
            replacement[key] = self._live_moves(record.get(key, []), fresh)
        try:
            if self.journal.read_bytes() != original:
                return {'state': 'conflict', 'reason': 'journal_changed_during_reconciliation'}
        except FileNotFoundError:
            return {'state': 'conflict', 'reason': 'journal_changed_during_reconciliation'}
        history = self.journal.parent / 'history'
        history.mkdir(parents=True, exist_ok=True, mode=0o700)
        archive = history / (self.journal.stem + '-' + time.strftime('%Y%m%d-%H%M%S') +
                             '-' + uuid4().hex + '.private.json')
        fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        if self._reconciliation_view(fresh) != self._reconciliation_view(self.snapshot()):
            return {'state': 'conflict', 'reason': 'audio_changed_during_reconciliation'}
        if not self.journal.exists() or self.journal.read_bytes() != original:
            return {'state': 'conflict', 'reason': 'journal_changed_during_reconciliation'}
        replacement['reconciled_from'] = str(archive)
        save_json(self.journal, replacement)
        return {'state': 'archived', 'archive': str(archive)}

    def _require_reconciled(self, result, data):
        if result['state'] != 'conflict':
            return
        record = read_json(self.journal)
        if record:
            self._check_defaults(record, data)
        raise ControlError('La sesión AEC cambió o contiene nodos sin propietario confirmado; actualiza y revisa las rutas.')

    def _check_defaults(self, record, data):
        if record['expected_defaults'] != [data['default_source'], data['default_sink']]:
            raise ControlError('Los predeterminados cambiaron fuera de la aplicación. No se sobrescribirán; revisa el diagnóstico.')

    def enable(self, source, sink, adopt=False):
        data = self.validate_pair(source, sink)
        reconciliation = self.reconcile(data)
        self._require_reconciled(reconciliation, data)
        data = self.validate_pair(source, sink)
        modules = self.echo_modules(data)
        if len(modules) > 1:
            raise ControlError('Hay varios módulos AEC. No se creará otro ni se eliminarán módulos ajenos.')
        record = self._remember(data)
        self._check_defaults(record, data)
        existing = modules[0] if modules else None
        if existing:
            args = arguments(existing)
            if args.get('source_master') != source or args.get('sink_master') != sink or args.get('aec_method') != 'webrtc':
                raise ControlError('El AEC existente utiliza otra ruta. Desactívalo desde su propietario antes de cambiarla.')
            owned = record.get('active') and self._same(record['active'], existing)
            if not owned and not adopt:
                raise ControlError('Existe un AEC ajeno. Usa «Administrar AEC existente» para asumir su control explícitamente.')
            module = existing
        else:
            names = {d['name'] for k in ('sources', 'sinks') for d in data[k]}
            if {'scl_mic_aec', 'scl_speakers_aec'} & names:
                raise ControlError('Nombres virtuales ocupados; no se crearán duplicados.')
            argument = (f'source_name=scl_mic_aec sink_name=scl_speakers_aec '
                        f'source_master={source} sink_master={sink} aec_method=webrtc')
            # Save intent first so a crash after load can be recovered by fingerprint.
            record['pending_argument'] = argument
            save_json(self.journal, record)
            module = self._load(argument)
        record['active'] = module
        record.pop('pending_argument', None)
        save_json(self.journal, record)
        args = arguments(module)
        try:
            latest = self.snapshot()
            self._check_defaults(record, latest)
            live = self.echo_modules(latest)
            if len(live) != 1 or not any(m.get('index') == module.get('index') and self._same(m, module) for m in live):
                raise ControlError('El inventario AEC cambió durante la activación; se conservan las rutas actuales.')
            self._defaults(args['source_name'], args['sink_name'])
        except Exception:
            if not existing:
                # Re-read identity before cleanup: module IDs can be reused.
                latest = self.snapshot()
                owned = next((m for m in self.echo_modules(latest)
                              if m.get('index') == module.get('index') and self._same(m, module)), None)
                selected = (latest['default_source'] == args['source_name'] or
                            latest['default_sink'] == args['sink_name'])
                if owned and not selected:
                    self.command('unload-module', owned['index'])
                    record['active'] = None
                    save_json(self.journal, record)
            raise
        record['expected_defaults'] = [args['source_name'], args['sink_name']]
        save_json(self.journal, record)
        return self.snapshot()

    def _active(self, record, data):
        saved = record.get('active')
        if not saved and record.get('pending_argument'):
            saved = {'name': 'module-echo-cancel', 'argument': record['pending_argument']}
        if not saved:
            return None
        matches = [m for m in self.echo_modules(data) if self._same(m, saved)]
        if len(matches) > 1:
            raise ControlError('Módulos duplicados: restauración automática detenida.')
        return matches[0] if matches else None

    def _unload_owned(self, module):
        live = self.items('modules')
        if not any(m.get('index') == module.get('index') and self._same(m, module) for m in live):
            raise ControlError('El módulo AEC cambió antes de retirarlo; no se eliminarán módulos ajenos.')
        self.command('unload-module', module['index'])

    def disable(self):
        data = self.snapshot()
        result = self.reconcile(data)
        self._require_reconciled(result, data)
        if not self.echo_modules(data) and result['state'] in ('none', 'current', 'archived'):
            latest = self.snapshot()
            if self.echo_modules(latest):
                raise ControlError('El inventario AEC cambió durante la desactivación; se conserva la sesión nueva.')
            return latest
        record = read_json(self.journal)
        if not record:
            raise ControlError('Activa o administra primero el AEC desde esta aplicación.')
        self._check_defaults(record, data)
        module = self._active(record, data)
        if not module:
            raise ControlError('El módulo administrado ya no existe. Revisa las rutas.')
        args = arguments(module)
        source, sink = record['before']['default_source'], record['before']['default_sink']
        if source == args['source_name']:
            source = args['source_master']
        if sink == args['sink_name']:
            sink = args['sink_master']
        for original in record['before']['modules']:
            original_args = arguments(original)
            if source == original_args.get('source_name') and source not in [s['name'] for s in data['sources']]:
                source = original_args['source_master']
            if sink == original_args.get('sink_name') and sink not in [s['name'] for s in data['sinks']]:
                sink = original_args['sink_master']
        if source not in [s['name'] for s in data['sources']] or sink not in [s['name'] for s in data['sinks']]:
            raise ControlError('La ruta de restauración no está disponible; se conserva AEC.')
        self._defaults(source, sink)
        record['expected_defaults'] = [source, sink]
        save_json(self.journal, record)
        self._undo_moves(record, data)
        self._evacuate_streams(record, self.snapshot(), args, source, sink)
        self._unload_owned(module)
        record['active'] = None
        save_json(self.journal, record)
        return self.snapshot()

    def _evacuate_streams(self, record, data, args, source, sink):
        # Streams attached to a virtual node must survive its removal. Preserve their
        # original destination and identity, including streams that predate this app.
        for kind, field, virtual, fallback in (
            ('source-outputs', 'source', args['source_name'], source),
            ('sink-inputs', 'sink', args['sink_name'], sink)):
            devices = data['sources' if field == 'source' else 'sinks']
            mapping = {d['name']: d['index'] for d in devices}
            for stream in data[kind]:
                if stream.get('client') is None or stream.get('owner_module') is not None:
                    continue
                if stream.get(field) != mapping.get(virtual):
                    continue
                move = {'kind': kind, 'field': field, 'index': stream['index'],
                        'client': stream['client'], 'from': virtual, 'to': fallback}
                record.setdefault('evacuated', []).append(move)
                save_json(self.journal, record)
                self.command('move-' + kind[:-1], stream['index'], fallback)

    def _restore_evacuated(self, record, data):
        temporary = {**record, 'moves': record.get('evacuated', [])}
        self._undo_moves(temporary, data)
        record['evacuated'] = []
        save_json(self.journal, record)

    def route_streams(self):
        data = self.snapshot()
        record = read_json(self.journal)
        if not record:
            raise ControlError('Activa primero AEC.')
        self._check_defaults(record, data)
        module = self._active(record, data)
        if not module:
            raise ControlError('No hay AEC administrado.')
        args = arguments(module)
        for kind, field, physical, virtual in (
            ('source-outputs', 'source', args['source_master'], args['source_name']),
            ('sink-inputs', 'sink', args['sink_master'], args['sink_name'])):
            devices = data['sources' if field == 'source' else 'sinks']
            mapping = {d['name']: d['index'] for d in devices}
            for stream in data[kind]:
                # Do not move the AEC's own streams or app metering streams (no client).
                if stream.get('client') is None or stream.get('owner_module') is not None or stream.get(field) != mapping.get(physical):
                    continue
                if stream.get('properties', {}).get('application.name') == 'Shure Control Linux':
                    continue
                move = {'kind': kind, 'field': field, 'index': stream['index'], 'client': stream['client'],
                        'from': physical, 'to': virtual}
                record['moves'].append(move)
                save_json(self.journal, record)
                self.command('move-' + kind[:-1], stream['index'], virtual)
        return self.snapshot()

    def _undo_moves(self, record, data):
        for move in record.get('moves', []):
            devices = data['sources' if move['field'] == 'source' else 'sinks']
            names = {d['name']: d['index'] for d in devices}
            stream = next((s for s in data[move['kind']] if s['index'] == move['index'] and s.get('client') == move['client']), None)
            if stream and stream.get(move['field']) == names.get(move['to']) and move['from'] in names:
                self.command('move-' + move['kind'][:-1], stream['index'], move['from'])
        record['moves'] = []
        save_json(self.journal, record)

    def restore(self):
        initial = self.snapshot()
        self._require_reconciled(self.reconcile(initial), initial)
        record = read_json(self.journal)
        if not record:
            raise ControlError('No hay cambios de audio guardados para restaurar.')
        data = self.snapshot()
        self._check_defaults(record, data)
        self._restore_volumes(record, data)
        active = self._active(record, data)
        originals = record['before']['modules']
        # Restore original modules first when they were explicitly adopted and removed.
        for original in originals:
            if not any(self._same(m, original) for m in self.echo_modules(data)):
                if self.echo_modules(data) and not active:
                    raise ControlError('Otro AEC ocupa la sesión. No se alterará.')
                self._load(original['argument'])
        self._defaults(record['before']['default_source'], record['before']['default_sink'])
        record['expected_defaults'] = [record['before']['default_source'], record['before']['default_sink']]
        save_json(self.journal, record)
        self._undo_moves(record, self.snapshot())
        self._restore_evacuated(record, self.snapshot())
        if active and not any(self._same(active, m) for m in originals):
            self._unload_owned(active)
        self.journal.unlink()
        return self.snapshot()

    def status(self, data):
        modules = self.echo_modules(data)
        if not modules:
            return 'Desactivado · sin módulos AEC'
        if len(modules) > 1:
            return f'Conflicto: {len(modules)} módulos AEC'
        args = arguments(modules[0])
        selected = data['default_source'] == args.get('source_name') and data['default_sink'] == args.get('sink_name')
        return ('Activo en las rutas predeterminadas' if selected else 'Cargado; revisar rutas predeterminadas') + ' · eficacia acústica no comprobada'
