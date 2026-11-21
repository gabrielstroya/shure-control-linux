#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Opt-in real PipeWire regression for an obsolete AEC journal.

Uses only a temporary journal. It does not access HID, record audio, restart
services, or write global configuration. Other applications should leave their
audio routes unchanged while this bounded test runs.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.audio.pipewire import PipeWire, arguments
from app.core import ControlError, read_json, run, save_json


SOURCE = 'alsa_input.usb-Shure_Inc_Shure_MV7-00.mono-fallback'
SINK = 'alsa_output.pci-0000_04_00.1.hdmi-stereo'
VIRTUALS = ['scl_mic_aec', 'scl_speakers_aec']


class AuditRunner:
    def __init__(self):
        self.writes = []
        self.created_indices = set()

    def __call__(self, command):
        if command[0] != 'pactl':
            raise AssertionError('Esta prueba solo permite comandos pactl.')
        operation = command[1]
        if operation not in ('--format=json', 'list', 'get-default-source', 'get-default-sink'):
            self.writes.append(list(command))
        result = run(command)
        if operation == 'load-module':
            self.created_indices.add(int(result))
        return result


def defaults(data):
    return [data['default_source'], data['default_sink']]


def module_fingerprints(data):
    return Counter((module['name'], module.get('argument', '')) for module in data['modules'])


def physical_state(data):
    return {
        kind: {
            device['name']: {
                'index': device['index'],
                'volume': {channel: value['value'] for channel, value in device.get('volume', {}).items()},
                'mute': device.get('mute'),
            }
            for device in PipeWire.physical(data, kind)
        }
        for kind in ('sources', 'sinks')
    }


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-audio-changes', action='store_true', required=True)
    parser.add_argument('--source', default=SOURCE)
    parser.add_argument('--sink', default=SINK)
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Ejecuta la prueba con el usuario habitual, sin root.')

    runner = AuditRunner()
    directory = Path(tempfile.mkdtemp(prefix='scl-aec-reconciliation-'))
    journal = directory / 'audio-session.json'
    manager = PipeWire(runner=runner, journal=journal)
    report = {
        'tests': [], 'passed': False, 'uid': os.geteuid(),
        'source': options.source, 'sink': options.sink,
        'acoustic_quality': 'not_tested', 'audio_recorded': False,
        'hardware_controls_written': False, 'real_user_journal_modified': False,
        'temporary_journal': str(journal),
    }
    before = None
    failure = None
    try:
        before = manager.validate_pair(options.source, options.sink)
        require(not manager.echo_modules(before), 'Se requiere una sesión sin módulos AEC activos.')
        require(defaults(before) == [options.source, options.sink],
                'Los predeterminados deben coincidir con el MV7 y la salida HDMI elegidos.')
        report['tests'].append('physical_mv7_hdmi_baseline_without_aec')

        stale = {
            'version': 1,
            'before': {
                'default_source': 'shure_sin_eco', 'default_sink': 'altavoces_sin_eco',
                'modules': [{
                    'name': 'module-echo-cancel', 'index': 536870916,
                    'argument': ('source_name=shure_sin_eco sink_name=altavoces_sin_eco '
                                 f'source_master={options.source} sink_master={options.sink} '
                                 'aec_method=webrtc'),
                }],
            },
            'active': None, 'moves': [],
            'expected_defaults': ['shure_sin_eco', 'altavoces_sin_eco'],
        }
        save_json(journal, stale)
        original_bytes = journal.read_bytes()
        try:
            manager._check_defaults(stale, before)
        except ControlError as error:
            require('Los predeterminados cambiaron fuera' in str(error),
                    'No se reprodujo el aviso esperado del diario antiguo.')
        else:
            raise AssertionError('El diario antiguo debería discrepar con los predeterminados actuales.')
        report['tests'].append('historical_defaults_warning_reproduced')

        writes_before = len(runner.writes)
        reconciliation = manager.reconcile(before)
        require(reconciliation['state'] == 'archived', 'El diario obsoleto no se reconcilió.')
        archive = Path(reconciliation['archive'])
        require(archive.read_bytes() == original_bytes, 'El respaldo no conserva exactamente el diario anterior.')
        require(stat.S_IMODE(archive.stat().st_mode) == 0o600, 'El respaldo debe usar permisos 0600.')
        require(len(runner.writes) == writes_before, 'La reconciliación emitió una escritura de audio.')
        reconciled = manager.snapshot()
        require(defaults(reconciled) == defaults(before), 'La reconciliación cambió los predeterminados.')
        require(module_fingerprints(reconciled) == module_fingerprints(before),
                'La reconciliación cambió módulos.')
        require(physical_state(reconciled) == physical_state(before),
                'La reconciliación cambió dispositivos físicos o sus volúmenes.')
        record = read_json(journal)
        require(record['before']['modules'] == [] and record['expected_defaults'] == defaults(before),
                'La nueva referencia no corresponde al estado físico real.')
        report['archive_sha256'] = hashlib.sha256(original_bytes).hexdigest()
        report['tests'].extend(['obsolete_journal_archived_exactly_with_mode_0600',
                                'reconciliation_performed_no_audio_writes'])

        active = manager.enable(options.source, options.sink)
        modules = manager.echo_modules(active)
        require(len(modules) == 1, 'La activación no produjo exactamente un módulo AEC.')
        module = modules[0]
        route = arguments(module)
        require(route.get('source_master') == options.source and route.get('sink_master') == options.sink
                and route.get('aec_method') == 'webrtc', 'El motor o la ruta AEC no son los solicitados.')
        require(defaults(active) == VIRTUALS, 'Los nodos virtuales no son los predeterminados activos.')
        require(all(name in [device['name'] for device in active[kind]]
                    for kind, name in zip(('sources', 'sinks'), VIRTUALS)),
                'Falta un nodo virtual AEC.')
        require(physical_state(active) == physical_state(before),
                'La activación alteró dispositivos físicos o sus volúmenes.')
        report['tests'].append('webrtc_activated_with_real_mv7_and_hdmi')

        duplicate = manager.enable(options.source, options.sink)
        duplicate_modules = manager.echo_modules(duplicate)
        require(len(duplicate_modules) == 1 and duplicate_modules[0]['index'] == module['index'],
                'La activación repetida duplicó o reemplazó el módulo.')
        report['tests'].append('repeated_enable_does_not_duplicate_module')

        manager = PipeWire(runner=runner, journal=journal)
        recovered = manager.snapshot()
        require(manager.reconcile(recovered)['state'] == 'current', 'No se recuperó la sesión vigente.')
        recovered_module = manager._active(read_json(journal), recovered)
        require(recovered_module and recovered_module['index'] == module['index'],
                'Un administrador nuevo no recuperó el módulo que administra.')
        report['tests'].append('new_manager_recovers_persisted_active_session')

        disabled = manager.disable()
        require(not manager.echo_modules(disabled), 'La desactivación dejó un módulo AEC.')
        require(defaults(disabled) == defaults(before), 'La desactivación no recuperó las rutas físicas.')
        require(not set(VIRTUALS) & {d['name'] for kind in ('sources', 'sinks') for d in disabled[kind]},
                'Quedan nodos virtuales de la prueba.')
        report['tests'].append('disable_removes_owned_aec_and_restores_physical_defaults')

        writes_before = len(runner.writes)
        manager.disable()
        require(len(runner.writes) == writes_before, 'La segunda desactivación debería ser inocua.')
        report['tests'].append('repeated_disable_without_aec_performs_no_audio_writes')

        loads_before = len(runner.created_indices)
        restored = manager.restore()
        require(not manager.echo_modules(restored) and len(runner.created_indices) == loads_before,
                'Restaurar resucitó el módulo AEC histórico.')
        require(not journal.exists(), 'El diario completado sigue activo.')
        report['tests'].append('restore_does_not_resurrect_historical_aec')

        manager = PipeWire(runner=runner, journal=journal)
        require(manager.reconcile()['state'] == 'none', 'Queda una sesión después de restaurar.')
        manager.enable(options.source, options.sink)
        require(len(manager.echo_modules(manager.snapshot())) == 1,
                'No se pudo iniciar una sesión nueva después de restaurar.')
        manager.disable()
        manager.restore()
        report['tests'].append('clean_manager_restart_allows_another_complete_aec_cycle')
    except Exception as error:
        failure = error
        report['error'] = f'{type(error).__name__}: {error}'
    finally:
        # Never use blanket unload or force the original defaults after a concurrent
        # external edit. Retain the temporary recovery journal if ownership/routing
        # cannot still be demonstrated from the live session.
        try:
            current = manager.snapshot()
            record = read_json(journal)
            modules = manager.echo_modules(current)
            if modules and record and before is not None:
                owned = manager._active(record, current)
                safe = (
                    len(modules) == 1 and owned is not None
                    and owned.get('index') in runner.created_indices
                    and defaults(current) == record.get('expected_defaults') == VIRTUALS
                    and record.get('before', {}).get('modules') == []
                    and [record['before'].get('default_source'), record['before'].get('default_sink')]
                    == defaults(before)
                )
                if safe:
                    manager.disable()
                    manager.restore()
                    report['cleanup'] = 'owned_session_restored'
                else:
                    report['cleanup'] = 'pending_external_change_or_unconfirmed_ownership'
            else:
                report['cleanup'] = 'no_active_aec_to_remove'
            after = manager.snapshot()
            report['no_aec_remaining'] = not manager.echo_modules(after)
            if before is not None:
                report['defaults_restored'] = defaults(before) == defaults(after)
                report['all_module_fingerprints_preserved'] = module_fingerprints(before) == module_fingerprints(after)
                report['physical_devices_volumes_and_mute_preserved'] = physical_state(before) == physical_state(after)
            report['audio_write_operations'] = len(runner.writes)
            final_checks = ('no_aec_remaining', 'defaults_restored', 'all_module_fingerprints_preserved',
                            'physical_devices_volumes_and_mute_preserved')
            clean = all(report.get(check) for check in final_checks)
            report['passed'] = failure is None and clean
            if clean:
                shutil.rmtree(directory)
                report['temporary_journal_retained'] = False
            else:
                report['temporary_journal_retained'] = True
        except Exception as error:
            report['cleanup_error'] = f'{type(error).__name__}: {error}'
            report['temporary_journal_retained'] = True
        if options.output:
            save_json(options.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
