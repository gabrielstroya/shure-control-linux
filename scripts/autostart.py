#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Manage only this user's optional XDG background autostart entry."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import ControlError
from app.desktop.settings import Autostart, DesktopSettings


def main():
    parser = argparse.ArgumentParser(description='Inicio automático opcional de Shure Control Linux')
    parser.add_argument('action', nargs='?', choices=('enable', 'disable', 'status'),
                        help='Sintaxis compatible con la versión anterior')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--enable', action='store_const', const='enable', dest='requested')
    actions.add_argument('--disable', action='store_const', const='disable', dest='requested')
    actions.add_argument('--status', action='store_const', const='status', dest='requested')
    parser.add_argument('--background', action='store_true',
                        help='El inicio se realiza siempre en segundo plano; se admite con enable')
    args = parser.parse_args()
    if args.action and args.requested:
        parser.error('Utiliza una sola acción: positional o --enable/--disable/--status.')
    action = args.action or args.requested
    if action is None:
        parser.error('Indica enable, disable o status.')
    if args.background and action != 'enable':
        parser.error('--background solo se admite al activar el inicio automático.')
    if os.getuid() == 0:
        raise SystemExit('Ejecuta como tu usuario habitual, sin sudo.')
    autostart, settings = Autostart(), DesktopSettings()
    try:
        saved = settings.load()
        if action == 'status':
            print(json.dumps({'enabled': autostart.is_enabled(), 'preference': saved['autostart'],
                              'entry': str(autostart.path), 'launch_mode': 'background'}, ensure_ascii=False, indent=2))
            return
        enabled = action == 'enable'
        autostart.set_enabled(enabled)
        settings.update(autostart=enabled)
    except (ControlError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
    print('Inicio automático activado en segundo plano.' if enabled else 'Inicio automático desactivado.')
    print('Entrada:', autostart.path)


if __name__ == '__main__':
    main()
