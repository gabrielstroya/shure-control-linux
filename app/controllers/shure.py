# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""MV7 original only. Unmodified shurectl CLI; no raw USB commands."""
import json
import math
import os
from pathlib import Path
import tempfile
import threading

from app.core import ControlError, run, save_json, state_dir

ROOT = Path(__file__).resolve().parents[2]
ENUMS = {
    'mode': ('manual', 'auto'),
    'eq_preset': ('flat', 'high_pass', 'presence_boost', 'high_pass_presence_boost'),
    'compressor': ('off', 'light', 'medium', 'heavy'),
    'auto_position': ('near', 'far'),
    'auto_tone': ('dark', 'natural', 'bright'),
}
BOOLS = {'limiter_enabled', 'led_live_meter', 'led_night_mode'}
FIELDS = set(ENUMS) | BOOLS | {'gain_db', 'monitor_mix'}


def validate(settings):
    if not isinstance(settings, dict) or set(settings) - FIELDS:
        raise ControlError('El perfil contiene parámetros no compatibles con MV7.')
    for key, value in settings.items():
        if key in ENUMS and value not in ENUMS[key]:
            raise ControlError(f'Valor no válido: {key}')
        if key in BOOLS and type(value) is not bool:
            raise ControlError(f'Valor no booleano: {key}')
        if key in ('gain_db', 'monitor_mix'):
            upper = 36 if key == 'gain_db' else 100
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= upper:
                raise ControlError(f'{key} fuera de rango.')
            if key == 'gain_db' and abs(value / 1.5 - round(value / 1.5)) > 0.001:
                raise ControlError('La ganancia MV7 utiliza pasos de 1,5 dB.')
            if key == 'monitor_mix' and int(value) != value:
                raise ControlError('Monitor Mix requiere un porcentaje entero.')
    return dict(settings)


class ShureController:
    def __init__(self, executable=None, runner=run, state_path=None):
        self.executable = str(executable or os.environ.get('SHURECTL_BIN') or
                              (ROOT / 'bin/shurectl' if (ROOT / 'bin/shurectl').exists()
                               else ROOT / 'integrations/shurectl/target/release/shurectl'))
        self.runner = runner
        self.lock = threading.RLock()
        self.identity = None
        self.last_before = None
        self.state_path = Path(state_path or state_dir() / 'hardware-before.json')

    def devices(self):
        devices = []
        for node in sorted(Path('/sys/class/hidraw').glob('hidraw*')):
            try:
                text = (node / 'device/uevent').read_text()
                if 'HID_ID=0003:000014ED:00001012' in text:
                    devices.append({'path': '/dev/' + node.name,
                                    'accessible': os.access('/dev/' + node.name, os.R_OK | os.W_OK)})
            except OSError:
                continue
        return devices

    def _call(self, args, env=None):
        selector = ['--device', self.identity] if self.identity else []
        text = self.runner([self.executable, *selector, *args, '--json'], env=env, timeout=35)
        try:
            state = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise ControlError('Respuesta del controlador no válida.') from exc
        if state.get('error'):
            raise ControlError(state['error'])
        if state.get('model') != 'Shure MV7':
            raise ControlError('Esta aplicación controla exclusivamente el MV7 original.')
        settings = state.get('settings')
        if not isinstance(settings, dict) or not {'mode', 'gain_db', 'monitor_mix'} <= settings.keys():
            raise ControlError('Lectura incompleta; controles bloqueados.')
        validate(settings)
        state['model'] = 'MV7'
        if type(state.get('muted')) is not bool:
            raise ControlError('Estado de mute desconocido; no se mostrará como silenciado.')
        return state

    def read(self):
        with self.lock:
            state = self._call([])
            serial = state.get('serial')
            # shurectl handles device selection. Retain a real serial, never a placeholder.
            if serial and serial not in ('Unknown', 'unknown', '—', 'N/A', ''):
                self.identity = serial
            return state

    def _backup(self, state):
        self.last_before = state
        if not self.state_path.exists():
            save_json(self.state_path, state)

    def mute(self, muted):
        if type(muted) is not bool:
            raise ControlError('Mute requiere un booleano.')
        with self.lock:
            before = self.read()
            self._backup(before)
            after = self._call(['--mute', 'on' if muted else 'off'])
            if after['muted'] != muted:
                raise ControlError('El dispositivo no confirmó el cambio de mute.')
            return after

    def apply(self, patch, expected=None):
        patch = validate(patch)
        with self.lock:
            before = self.read()
            if expected is not None and (before['settings'] != expected['settings'] or
                                         before.get('serial') != expected.get('serial')):
                raise ControlError('El micrófono cambió desde la lectura. Actualiza antes de aplicar.')
            missing = patch.keys() - before['settings'].keys()
            if missing:
                raise ControlError('El dispositivo no ha podido leer: ' + ', '.join(sorted(missing)))
            mode = patch.get('mode', before['settings']['mode'])
            if mode == 'auto' and any(k in patch for k in ('gain_db', 'eq_preset', 'compressor', 'limiter_enabled')):
                raise ControlError('Ganancia, EQ y dinámica requieren modo manual.')
            if mode == 'manual' and any(k in patch for k in ('auto_position', 'auto_tone')):
                raise ControlError('Near/Far y tono automático requieren modo automático.')
            self._backup(before)
            settings = {**before['settings'], **patch}
            # A fresh state becomes a private temporary preset. Upstream only writes diffs.
            # Own XDG directory never touches the user's four shurectl preset slots.
            values = {'name': 'Shure Control Linux', **settings, 'monitor_mix_is_mic_level': False}
            with tempfile.TemporaryDirectory(prefix='shure-control-') as directory:
                path = Path(directory) / 'shurectl/presets/preset_1.toml'
                path.parent.mkdir(parents=True, mode=0o700)
                path.write_text('\n'.join(f'{key} = {json.dumps(value)}' for key, value in values.items()) + '\n')
                path.chmod(0o600)
                after = self._call(['--preset', '1'], env={'XDG_CONFIG_HOME': directory})
            for key, value in patch.items():
                # MV7 1/2% mix is the same hardware value as 0%.
                target = 0 if key == 'monitor_mix' and value in (1, 2) else value
                if after['settings'].get(key) != target:
                    raise ControlError(f'El MV7 no confirmó {key}. Estado releído; puede haber cambios parciales.')
            return after

    def restore(self, snapshot):
        with self.lock:
            current = self.read()
            if snapshot.get('model') != 'MV7' or snapshot.get('serial') != current.get('serial'):
                raise ControlError('La copia pertenece a otro micrófono.')
            settings = dict(snapshot['settings'])
            # Settings inactive in the target mode are managed by the hardware.
            omit = ('gain_db', 'eq_preset', 'compressor', 'limiter_enabled') if settings['mode'] == 'auto' else ('auto_position', 'auto_tone')
            for key in omit:
                settings.pop(key, None)
            result = self.apply(settings)
            if result['muted'] != snapshot['muted']:
                result = self.mute(snapshot['muted'])
            return result
