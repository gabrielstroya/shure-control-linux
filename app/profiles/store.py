# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Profiles are local audio settings, never integrations with named services."""
from pathlib import Path
from app.core import ControlError, config_dir, read_json, save_json
from app.controllers.shure import validate

PRESETS = [
    ('whatsapp', 'WhatsApp', True, {'mode': 'auto', 'auto_position': 'near', 'auto_tone': 'natural'}),
    ('teams', 'Microsoft Teams', True, {'mode': 'auto', 'auto_position': 'near', 'auto_tone': 'natural'}),
    ('meet', 'Google Meet', True, {'mode': 'auto', 'auto_position': 'near', 'auto_tone': 'natural'}),
    ('recording', 'Grabación', False, {'mode': 'manual', 'eq_preset': 'flat', 'compressor': 'off'}),
    ('hugo', 'Hugo / AmparoLex', True, {'mode': 'auto', 'auto_position': 'near', 'auto_tone': 'natural'}),
    ('video', 'Videollamada', True, {'mode': 'auto', 'auto_position': 'near', 'auto_tone': 'natural'}),
    ('narration', 'Narración', False, {'mode': 'manual', 'eq_preset': 'presence_boost', 'compressor': 'light'}),
    ('elevenlabs', 'ElevenLabs', False, {'mode': 'manual', 'eq_preset': 'flat', 'compressor': 'off', 'limiter_enabled': False}),
    ('custom', 'Personalizado', False, {}),
]


class Profiles:
    def __init__(self, path=None):
        self.path = Path(path or config_dir() / 'profiles.json')

    def all(self):
        saved = read_json(self.path, {})
        if not isinstance(saved, dict) or any(not isinstance(value, dict) for value in saved.values()):
            raise ControlError('El archivo de perfiles no tiene un formato válido; se conserva sin modificar.')
        return [{**{'id': key, 'name': name, 'aec': aec, 'settings': settings, 'model': 'MV7', 'customized': False},
                 **saved.get(key, {})} for key, name, aec, settings in PRESETS]

    def save(self, key, state, source, sink, aec):
        if key not in [p[0] for p in PRESETS] or state.get('model') != 'MV7':
            raise ControlError('Perfil o modelo no válido.')
        settings = validate(state['settings'])
        if settings['mode'] == 'auto':
            for field in ('gain_db', 'compressor', 'eq_preset', 'limiter_enabled'):
                settings.pop(field, None)
        else:
            for field in ('auto_position', 'auto_tone'):
                settings.pop(field, None)
        saved = read_json(self.path, {})
        saved[key] = {'settings': settings, 'source': source, 'sink': sink,
                      'aec': bool(aec), 'model': 'MV7', 'customized': True}
        save_json(self.path, saved)

    def get(self, key):
        profile = next((p for p in self.all() if p['id'] == key), None)
        if profile is None or profile['model'] != 'MV7':
            raise ControlError('Perfil no compatible.')
        validate(profile['settings'])
        return profile
