# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Optional desktop integration; all state belongs to the current user."""
import os
from pathlib import Path
import tempfile

from app.core import ControlError, config_dir, read_json, save_json

APP_ID = 'io.github.shurecontrol.Linux'
DEFAULTS = {
    'resident_enabled': False,
    'close_behavior': 'ask',
    'keep_aec_on_exit': True,
    'autostart': False,
    'shortcuts_enabled': False,
}
CLOSE_BEHAVIORS = ('ask', 'quit', 'hide', 'resident')


class DesktopSettings:
    """Keep desktop settings separate from the audio routing preferences."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else config_dir() / 'desktop.json'

    @staticmethod
    def _validate(values):
        for key, value in values.items():
            if key not in DEFAULTS:
                continue
            if key == 'close_behavior':
                valid = value in CLOSE_BEHAVIORS
            else:
                valid = type(value) is bool
            if not valid:
                raise ControlError(f'Preferencia de escritorio no válida: {key}.')

    def load(self):
        saved = read_json(self.path, {})
        if not isinstance(saved, dict):
            raise ControlError('Las preferencias de escritorio no tienen un formato válido; se conservan sin modificar.')
        self._validate(saved)
        return {**DEFAULTS, **saved}

    def update(self, **kwargs):
        unknown = set(kwargs) - set(DEFAULTS)
        if unknown:
            raise ControlError('Preferencia desconocida: ' + ', '.join(sorted(unknown)))
        self._validate(kwargs)
        # Re-read before writing so another preference change is retained.
        settings = {**self.load(), **kwargs}
        save_json(self.path, settings)
        return settings


class Autostart:
    """Own exactly one XDG autostart entry, never a service or other entries."""

    marker = f'X-ShureControlLinux-Owner={APP_ID}'

    def __init__(self, path=None, launcher=None):
        root = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
        self.path = Path(path) if path is not None else root / 'autostart' / f'{APP_ID}.desktop'
        self.launcher = Path(launcher) if launcher is not None else Path.home() / '.local/bin/shure-control-linux'

    def _owned(self):
        if self.path.is_symlink():
            raise ControlError('La entrada de inicio automático es un enlace ajeno; se conserva.')
        if not self.path.exists():
            return False
        try:
            lines = self.path.read_text().splitlines()
        except (OSError, UnicodeError) as exc:
            raise ControlError('No se pudo comprobar la entrada de inicio automático.') from exc
        if self.marker not in lines:
            raise ControlError('La entrada de inicio automático no tiene la marca de Shure Control Linux; se conserva.')
        return True

    def is_enabled(self):
        if not self._owned():
            return False
        lines = self.path.read_text().splitlines()
        return 'Hidden=true' not in lines and 'X-GNOME-Autostart-enabled=false' not in lines

    @staticmethod
    def _quoted_exec(path):
        value = str(path)
        if any(char in value for char in '\r\n\x00'):
            raise ControlError('La ruta del lanzador contiene caracteres no admitidos.')
        # Desktop Entry strings undergo general unescaping followed by Exec
        # quoting. Four backslashes encode one literal backslash in Exec.
        value = value.replace('\\', '\\\\\\\\')
        for char in ('"', '`', '$'):
            value = value.replace(char, '\\\\' + char)
        return '"' + value.replace('%', '%%') + '"'

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ControlError('El inicio automático debe activarse o desactivarse con un valor booleano.')
        owned = self._owned()
        if not enabled:
            if owned:
                self.path.unlink()
            return False
        if not self.launcher.is_file() or not os.access(self.launcher, os.X_OK):
            raise ControlError('Instala primero Shure Control Linux para habilitar el inicio automático.')
        try:
            if APP_ID not in self.launcher.read_text():
                raise ControlError('El lanzador existente no pertenece a Shure Control Linux.')
        except (OSError, UnicodeError) as exc:
            raise ControlError('No se pudo comprobar el lanzador instalado.') from exc
        content = (
            '[Desktop Entry]\nVersion=1.0\nType=Application\n'
            'Name=Shure Control Linux\n'
            'Comment=Control residente local del Shure MV7\n'
            f'Exec={self._quoted_exec(self.launcher)} --background\n'
            f'Icon={APP_ID}\nTerminal=false\nStartupNotify=false\n'
            f'X-GNOME-Autostart-enabled=true\n{self.marker}\n'
        )
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.' + self.path.name)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True
