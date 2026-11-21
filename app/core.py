# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Local process execution and private, atomic state storage."""
import json
import os
from pathlib import Path
import subprocess
import tempfile


class ControlError(RuntimeError):
    pass


def run(args, *, env=None, timeout=20):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                env={**os.environ, 'LC_ALL': 'C.UTF-8', **(env or {})})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControlError(f'No se pudo ejecutar {Path(args[0]).name}: {exc}') from exc
    if result.returncode:
        raise ControlError((result.stderr or result.stdout or 'Operación fallida').strip()[:1800])
    return result.stdout.strip()


def config_dir():
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'shure-control-linux'


def state_dir():
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'shure-control-linux'


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as exc:
        raise ControlError(f'No se pudo leer {Path(path).name}: {exc}') from exc
