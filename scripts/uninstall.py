#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Remove only this app. Preserve settings/backups and udev unless removed explicitly."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import sys

APP_ID = 'io.github.shurecontrol.Linux'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core import ControlError
from app.desktop.settings import Autostart


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if os.getuid() == 0:
        raise SystemExit('Ejecuta sin sudo.')
    base = Path.home() / '.local/share/shure-control-linux'
    marker = base / '.installation.json'
    if base.is_symlink():
        raise SystemExit('El directorio de instalación es un enlace inesperado; se conserva.')
    if not marker.exists():
        raise SystemExit('Instalación propia no encontrada; no se elimina nada.')
    metadata = json.loads(marker.read_text())
    if metadata.get('application_id') != APP_ID:
        raise SystemExit('Instalación propia no encontrada; no se elimina nada.')
    if '--restore-audio' in sys.argv:
        import subprocess
        subprocess.run([str(Path.home() / '.local/bin/shure-control-linux'), '--restore-audio'], check=True)
    installed_files = metadata.get('installed_files', {})
    launcher = Path.home() / '.local/bin/shure-control-linux'
    desktop = Path.home() / f'.local/share/applications/{APP_ID}.desktop'
    icons = [Path.home() / f'.local/share/icons/hicolor/scalable/apps/{APP_ID}{suffix}.svg'
             for suffix in ('', '-connected', '-muted', '-disconnected', '-error')]
    # Whitelist paths; metadata cannot authorize deleting arbitrary files.
    for path in [launcher, desktop, *icons]:
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink():
            print('Se conserva un enlace ajeno:', path)
            continue
        expected = installed_files.get(str(path))
        if expected:
            owned = digest(path) == expected
        elif path == launcher:
            owned = '# ' + APP_ID in path.read_text().splitlines()
        elif path == desktop:
            owned = 'StartupWMClass=' + APP_ID in path.read_text().splitlines()
        else:
            source = base / 'current/packaging/icons' / path.name
            owned = source.is_file() and digest(path) == digest(source)
        if owned:
            path.unlink()
        else:
            print('Se conserva un archivo modificado o ajeno:', path)
    try:
        Autostart().set_enabled(False)
    except (ControlError, OSError) as exc:
        print('Se conserva la entrada de inicio automático:', exc)
    shutil.rmtree(base)
    print('Aplicación eliminada. Se conservan perfiles, copias y regla udev. Consulta docs/MAINTENANCE.md.')


if __name__ == '__main__':
    main()
