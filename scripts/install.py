#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Install per user. No sudo, no pip/global package writes, no autostart by default."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
APP_ID = 'io.github.shurecontrol.Linux'
VERSION = '0.2.1'
ICON_SUFFIXES = ('', '-connected', '-muted', '-disconnected', '-error')
sys.path.insert(0, str(ROOT))
from app.core import save_json
from app.desktop.settings import Autostart


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path, contents, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    if os.getuid() == 0:
        raise SystemExit('Ejecuta el instalador como tu usuario habitual, sin sudo.')
    subprocess.run(['/usr/bin/python3', '-c', 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1"); from gi.repository import Gtk,Adw'], check=True)
    binary = ROOT / 'bin/shurectl'
    if not binary.exists():
        binary = ROOT / 'integrations/shurectl/target/release/shurectl'
    if not binary.exists():
        raise SystemExit('Compila primero shurectl con scripts/build-controller.sh.')
    for tool in ('pactl', 'parec'):
        if not shutil.which(tool):
            raise SystemExit('Falta una dependencia del sistema: ' + tool)
    base = Path.home() / '.local/share/shure-control-linux'
    marker = base / '.installation.json'
    if base.is_symlink():
        raise SystemExit('El directorio de instalación es un enlace inesperado; se conserva.')
    previous_metadata = {}
    if marker.exists():
        previous_metadata = json.loads(marker.read_text())
    if base.exists() and previous_metadata.get('application_id') != APP_ID:
        raise SystemExit('El destino existe y no pertenece a este instalador: ' + str(base))
    current = base / 'current'
    if current.exists() and not current.is_symlink():
        raise SystemExit('No se sustituirá un directorio current ajeno.')
    previous_release = os.readlink(current) if current.is_symlink() else None
    launcher = Path.home() / '.local/bin/shure-control-linux'
    desktop = Path.home() / f'.local/share/applications/{APP_ID}.desktop'
    icons = [Path.home() / f'.local/share/icons/hicolor/scalable/apps/{APP_ID}{suffix}.svg'
             for suffix in ICON_SUFFIXES]
    # Preflight every public entry before creating or selecting the new release.
    for target, identity in ((launcher, '# ' + APP_ID), (desktop, 'StartupWMClass=' + APP_ID)):
        if target.is_symlink() or (target.exists() and identity not in target.read_text().splitlines()):
            raise SystemExit('Existe una entrada ajena; se conserva: ' + str(target))
    previous_files = previous_metadata.get('installed_files', {})
    for icon in icons:
        source_icon = ROOT / 'packaging/icons' / icon.name
        if not source_icon.is_file():
            raise SystemExit('Falta un icono de la aplicación: ' + str(source_icon))
        if icon.is_symlink():
            raise SystemExit('Existe un icono enlazado ajeno; se conserva: ' + str(icon))
        if icon.exists():
            old_source = current / 'packaging/icons' / icon.name
            owned_hash = previous_files.get(str(icon))
            if not owned_hash and old_source.is_file():
                owned_hash = digest(old_source)
            if digest(icon) != owned_hash:
                raise SystemExit('Existe un icono modificado o ajeno; se conserva: ' + str(icon))
    base.mkdir(parents=True, exist_ok=True)
    release = base / 'releases' / datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    release.mkdir(parents=True)
    for name in ('app', 'packaging', 'scripts', 'docs', 'tests', 'locale'):
        if (ROOT / name).exists():
            shutil.copytree(ROOT / name, release / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.private.*'))
    for name in ('README.md', 'LICENSE', 'COPYRIGHT', 'THIRD_PARTY_NOTICES.md',
                 'CONTRIBUTING.md', 'SECURITY.md', 'pyproject.toml'):
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, release / name)
    (release / 'bin').mkdir()
    shutil.copy2(binary, release / 'bin/shurectl')
    # Ship the corresponding unmodified GPL source beside the compiled binary.
    upstream = ROOT / 'integrations/shurectl'
    if upstream.exists():
        shutil.copytree(upstream, release / 'integrations/shurectl',
                        ignore=shutil.ignore_patterns('.git', 'target', '__pycache__', '*.pyc', '*.private.*'))
    subprocess.run(['/usr/bin/python3', '-m', 'venv', '--without-pip', '--system-site-packages', str(release / '.venv')], check=True)
    # Python launcher avoids shell interpolation of user paths.
    atomic_write(launcher, '#!/usr/bin/python3\n# ' + APP_ID + '\nimport os,sys\nfrom pathlib import Path\np=Path.home()/".local/share/shure-control-linux/current"\nos.chdir(p)\nos.execv(str(p/".venv/bin/python3"), [str(p/".venv/bin/python3"),"-m","app.main",*sys.argv[1:]])\n', 0o755)
    for icon in icons:
        atomic_write(icon, (ROOT / 'packaging/icons' / icon.name).read_text(), 0o644)
    quoted = Autostart._quoted_exec(launcher)
    atomic_write(desktop, f'[Desktop Entry]\nVersion=1.0\nType=Application\nName=Shure Control Linux\nComment=Control local del Shure MV7 y cancelación de eco\nExec={quoted}\nIcon={APP_ID}\nTerminal=false\nCategories=AudioVideo;Audio;\nStartupNotify=true\nStartupWMClass={APP_ID}\nX-ShureControlLinux-Owner={APP_ID}\n', 0o644)
    temporary = base / ('.current-' + uuid4().hex)
    temporary.symlink_to(release, target_is_directory=True)
    temporary.replace(current)
    save_json(marker, {'application_id': APP_ID, 'version': VERSION, 'source': str(ROOT),
                       'current_release': str(release), 'previous_release': previous_release,
                       'installed_files': {str(path): digest(path) for path in [launcher, desktop, *icons]}})
    if shutil.which('update-desktop-database'):
        subprocess.run(['update-desktop-database', str(desktop.parent)], check=True)
    print('Instalado para tu usuario. Lanzador:', launcher)
    print('Menú:', desktop)
    print('Versión:', VERSION)
    print('Release:', release)
    if previous_release:
        print('Release anterior conservada para reversión:', previous_release)
    print('Inicio automático y preferencias de residente conservados sin cambios.')


if __name__ == '__main__':
    main()
