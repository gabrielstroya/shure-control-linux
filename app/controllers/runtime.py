# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""One shared controller and worker for window, indicator and shortcuts.

Only state reads run periodically. Capture and DSP writes require an explicit action.
"""
from concurrent.futures import ThreadPoolExecutor
import logging
import time
from gi.repository import GLib

from app.audio.meter import Meter
from app.audio.pipewire import PipeWire, arguments
from app.controllers.shure import ShureController
from app.core import ControlError, config_dir, read_json, save_json
from app.profiles.store import Profiles


DEFAULTS_CONFLICT_NOTICE = 'Los predeterminados cambiaron fuera de la aplicación.'


def is_defaults_conflict(message):
    return isinstance(message, str) and message.startswith(DEFAULTS_CONFLICT_NOTICE)


class ControllerRuntime:
    def __init__(self, shure=None, audio=None, profiles=None, meter=None):
        self.shure = shure or ShureController()
        self.audio = audio or PipeWire()
        self.profiles = profiles or Profiles()
        self.meter = meter or Meter()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='shure-control')
        self.hardware = None
        self.hardware_error = None
        self.audio_data = None
        self.audio_error = None
        self.audio_reconciliation = None
        self.last_message = None
        self.operation_error = None
        self.devices = []
        self.previous_hardware = None
        self.last_profile = None
        self.updated_at = None
        self.polling = False
        self.action_busy = False
        self.closed = False
        self.poll_id = 0
        self.measure_timer = 0
        self.meter_text = 'Sin captura · medir durante 5 s'
        self.listeners = []
        self.message_listeners = []
        self.preferences_path = config_dir() / 'preferences.json'
        try:
            self.preferences = read_json(self.preferences_path, {})
        except ControlError:
            self.preferences = {}

    @property
    def busy(self):
        return self.polling or self.action_busy

    def start(self):
        if not self.poll_id:
            self.refresh()
            self.poll_id = GLib.timeout_add_seconds(4, self.refresh)

    def emit(self):
        for callback in list(self.listeners):
            callback(self)

    def notify(self, message):
        # None explicitly withdraws an obsolete notification/toast.
        self.last_message = message
        for callback in list(self.message_listeners):
            callback(message)

    def _clear_resolved_audio_notice(self):
        state = (self.audio_reconciliation or {}).get('state')
        if self.audio_data is None or self.audio_error or state not in ('none', 'current', 'archived'):
            return
        error_cleared = is_defaults_conflict(self.operation_error)
        message_cleared = is_defaults_conflict(self.last_message)
        if error_cleared:
            self.operation_error = None
        if message_cleared:
            self.notify(None)
        elif state == 'archived' and self.last_message is None and self.operation_error is None:
            # A journal can outlive its old process and desktop notification.
            self.notify(None)

    def refresh(self):
        if self.closed:
            return False
        if self.busy:
            return True
        self.polling = True
        def task():
            try:
                hardware, error = self.shure.read(), None
            except Exception as exc:
                hardware, error = None, str(exc)
            reconciliation = None
            try:
                audio, audio_error = self.audio.snapshot(), None
            except Exception as exc:
                audio, audio_error = None, str(exc)
            if audio is not None:
                try:
                    reconcile = getattr(self.audio, 'reconcile', None)
                    if reconcile is not None:
                        reconciliation = reconcile(audio)
                except Exception as exc:
                    # A journal problem does not invalidate the live inventory
                    # or turn an independently connected MV7 into a USB error.
                    audio_error = str(exc)
            return hardware, error, audio, audio_error, self.shure.devices(), reconciliation
        future = self.executor.submit(task)
        def done():
            self.polling = False
            if self.closed:
                return False
            try:
                (self.hardware, self.hardware_error, self.audio_data, self.audio_error,
                 self.devices, self.audio_reconciliation) = future.result()
                self.updated_at = time.time()
                self._clear_resolved_audio_notice()
            except Exception as exc:
                self.hardware = None
                self.hardware_error = str(exc)
                exc.__traceback__ = None
            self.emit()
            return False
        future.add_done_callback(lambda _f: GLib.idle_add(done))
        return True

    def work(self, message, task, on_result=None):
        if self.closed:
            return False
        if self.action_busy:
            self.notify('Espera a que termine la operación anterior.')
            return False
        self.action_busy = True
        self.emit()
        future = self.executor.submit(task)
        def done():
            self.action_busy = False
            if self.closed:
                return False
            try:
                result = future.result()
                if on_result:
                    on_result(result)
                if self.closed:
                    return False
                # A defaults conflict is cleared only after current audio state
                # proves it resolved, not by an unrelated successful operation.
                if not is_defaults_conflict(self.operation_error):
                    self.operation_error = None
                self.notify(message)
                logging.info('Operación completada: %s', message)
            except Exception as exc:
                if self.shure.last_before:
                    self.previous_hardware = self.shure.last_before
                self.operation_error = str(exc)
                self.notify(str(exc))
                logging.warning('Operación fallida: %s', type(exc).__name__)
                # Future retains its exception. Drop handled traceback frames so
                # a nested CLI wait does not retain the remote command object.
                exc.__traceback__ = None
            self.refresh()
            self.emit()
            return False
        future.add_done_callback(lambda _f: GLib.idle_add(done))
        return True

    def remember_hardware(self, result):
        self.previous_hardware, self.hardware = result

    def toggle_mute(self):
        def task():
            before = self.shure.read()
            return before, self.shure.mute(not before['muted'])
        return self.work('Mute confirmado por el MV7', task, self.remember_hardware)

    def adjust_gain(self, delta):
        if delta not in (-1.5, 1.5):
            raise ControlError('El ajuste rápido debe ser de 1,5 dB.')
        def task():
            before = self.shure.read()
            if before['settings']['mode'] != 'manual':
                raise ControlError('La ganancia interna solo se ajusta en modo manual.')
            gain = min(36, max(0, before['settings']['gain_db'] + delta))
            return before, self.shure.apply({'gain_db': gain}, expected=before)
        return self.work('Ganancia confirmada por el MV7', task, self.remember_hardware)

    def selected_source(self):
        if self.audio_data is None:
            raise ControlError('Sin inventario de audio disponible.')
        names = [d['name'] for d in self.audio.physical(self.audio_data, 'sources')]
        selected = self.preferences.get('source')
        if selected not in names:
            selected = next((name for name in names if 'shure' in name.lower()), None)
        if selected is None:
            raise ControlError('Selecciona una entrada física disponible en «Eco y rutas».')
        return selected

    def selected_pair(self, data=None):
        data = data or self.audio_data
        if data is None:
            raise ControlError('Sin inventario de audio disponible.')
        result = []
        for kind, preference, match in [('sources', 'source', 'shure'), ('sinks', 'sink', 'hdmi')]:
            names = [d['name'] for d in self.audio.physical(data, kind)]
            selected = self.preferences.get(preference)
            if selected not in names:
                selected = next((name for name in names if match in name.lower()), None)
            if selected is None:
                raise ControlError('Selecciona explícitamente el micrófono y los altavoces en «Eco y rutas».')
            result.append(selected)
        return tuple(result)

    def save_pair(self, source, sink):
        self.preferences.update(source=source, sink=sink)
        save_json(self.preferences_path, self.preferences)

    def aec_state(self):
        if self.audio_data is None:
            return 'unknown', False
        modules = self.audio.echo_modules(self.audio_data)
        if len(modules) > 1:
            return 'conflict', False
        if not modules:
            return 'off', False
        try:
            record = read_json(self.audio.journal)
            active = self.audio._active(record, self.audio_data) if record else None
            args = arguments(modules[0])
            selected = self.audio_data['default_source'] == args.get('source_name') and self.audio_data['default_sink'] == args.get('sink_name')
            return ('active' if selected else 'loaded'), bool(active)
        except (ControlError, KeyError, TypeError):
            return 'unknown', False

    def toggle_aec(self):
        def task():
            data = self.audio.snapshot()
            modules = self.audio.echo_modules(data)
            if modules:
                return self.audio.disable()
            source, sink = self.selected_pair(data)
            return self.audio.enable(source, sink)
        return self.work('AEC actualizado; eficacia acústica pendiente de comprobar', task)

    def apply_profile(self, key, source=None, sink=None):
        def task():
            profile = self.profiles.get(key)
            if key == 'custom' and not profile['customized']:
                raise ControlError('Guarda primero tus ajustes en el perfil Personalizado.')
            data = self.audio.snapshot()
            if profile['aec']:
                selected = (source, sink) if source and sink else self.selected_pair(data)
                mic, output = profile.get('source', selected[0]), profile.get('sink', selected[1])
                self.audio.validate_pair(mic, output, data)
            before = self.shure.read()
            modules = self.audio.echo_modules(data)
            record = read_json(self.audio.journal)
            if modules and not (record and self.audio._active(record, data)):
                raise ControlError('Administra primero el AEC existente en «Eco y rutas».')
            after = self.shure.apply(profile['settings'])
            try:
                if profile['aec']:
                    self.audio.enable(mic, output)
                elif modules:
                    self.audio.disable()
            except Exception as exc:
                try:
                    self.shure.restore(before)
                except Exception as rollback:
                    raise ControlError(f'Perfil parcial: {exc}. Restauración pendiente: {rollback}') from exc
                raise
            return before, after
        def done(result):
            self.remember_hardware(result)
            self.last_profile = key
        return self.work('Perfil aplicado; mute conservado', task, done)

    def start_measurement(self, seconds=5):
        if self.closed or self.action_busy:
            return
        if self.meter.process or self.measure_timer:
            self.notify('Ya hay una medición activa en la ventana.')
            return
        try:
            source = self.selected_source()
        except ControlError as exc:
            self.notify(str(exc))
            return
        self.meter.visuals_enabled = False
        self.meter.start(source)
        if self.meter.error or not self.meter.process:
            self.meter_text = 'Medición no disponible: ' + (self.meter.error or 'No se pudo iniciar la captura.')
            self.emit()
            self.notify(self.meter_text)
            return
        self.meter_text = 'Midiendo durante 5 s · audio solo en memoria'
        self.emit()
        self.measure_timer = GLib.timeout_add_seconds(seconds, self.finish_measurement)

    def finish_measurement(self):
        self.measure_timer = 0
        text = ('Medición no disponible: ' + self.meter.error if self.meter.error else
                f'Última medida · Peak {self.meter.peak:.1f} / RMS {self.meter.rms:.1f} dBFS')
        self.meter.stop()
        self.meter_text = text
        self.emit()
        self.notify(text)
        return False

    def stop_meter(self):
        if self.measure_timer:
            GLib.source_remove(self.measure_timer)
            self.measure_timer = 0
        self.meter.visuals_enabled = False
        self.meter.stop()
        self.meter_text = 'Sin captura · medir durante 5 s'
        self.emit()

    def snapshot(self, resident=False):
        aec_state, managed = self.aec_state()
        try:
            profiles = [{'id': p['id'], 'name': p['name']} for p in self.profiles.all()]
        except ControlError:
            profiles = []
        return {'hardware': self.hardware, 'hardware_error': self.hardware_error,
                'audio_error': self.audio_error, 'operation_error': self.operation_error,
                'usb_present': bool(self.devices), 'busy': self.action_busy,
                'aec_state': aec_state, 'aec_managed': managed, 'profiles': profiles,
                'meter_text': self.meter_text, 'resident': resident, 'updated_at': self.updated_at}

    def shutdown(self):
        if self.busy:
            raise ControlError('Hay una operación en curso; espera antes de salir.')
        self.closed = True
        if self.poll_id:
            GLib.source_remove(self.poll_id)
            self.poll_id = 0
        self.stop_meter()
        self.executor.shutdown(wait=True, cancel_futures=True)
