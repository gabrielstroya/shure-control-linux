# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Local, privacy-minimized diagnostic export. No network code."""
import datetime
import platform
from pathlib import Path
from app.core import ControlError, run, save_json
from app import __version__


def report(pipewire, hardware, hardware_error=None):
    data = pipewire.snapshot()
    def device(d):
        return {key: d.get(key) for key in ('index', 'name', 'description', 'sample_specification', 'state', 'mute', 'volume')}
    def streams(kind, field):
        return [{'index': s['index'], 'device_index': s.get(field),
                 'application': s.get('properties', {}).get('application.name', 'Desconocida'),
                 'pid': s.get('properties', {}).get('application.process.id')}
                for s in data[kind]]
    try:
        version = run(['pipewire', '--version'])
    except ControlError as exc:
        version = str(exc)
    safe_hardware = {k: v for k, v in (hardware or {}).items() if k != 'serial'}
    alsa = Path('/proc/asound/cards')
    return {'application': 'Shure Control Linux ' + __version__,
            'timestamp_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'os': platform.freedesktop_os_release().get('PRETTY_NAME', platform.system()),
            'pipewire': version,
            'alsa_cards': alsa.read_text() if alsa.exists() else 'No disponible',
            'hardware': safe_hardware or {'error': hardware_error or 'Sin lectura'},
            'sources': list(map(device, data['sources'])), 'sinks': list(map(device, data['sinks'])),
            'default_source': data['default_source'], 'default_sink': data['default_sink'],
            'aec_modules': pipewire.echo_modules(data), 'aec_status': pipewire.status(data),
            'acoustic_validation': 'PENDIENTE: requiere prueba física o videollamada con interlocutor.',
            'capture_clients': streams('source-outputs', 'source'),
            'playback_clients': streams('sink-inputs', 'sink'),
            'privacy': 'Informe local sin audio, credenciales ni número de serie del MV7. Identificadores de rutas y nombres locales de dispositivos y aplicaciones incluidos.'}


def export(path, data):
    save_json(path, data)
    return str(path)
