# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Desktop entry point. No services, no automatic audio changes on launch."""
import argparse
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from app.core import state_dir
from app import __version__


def main():
    parser = argparse.ArgumentParser(description='Shure Control Linux')
    parser.add_argument('--version', action='version', version='Shure Control Linux ' + __version__)
    parser.add_argument('--diagnose', action='store_true', help='Emitir diagnóstico local JSON')
    parser.add_argument('--restore-audio', action='store_true', help='Restaurar la sesión de audio anterior')
    parser.add_argument('--screenshot', help=argparse.SUPPRESS)
    for flag in ('background', 'show', 'toggle-mute', 'next-profile', 'quit', 'status'):
        parser.add_argument('--' + flag, action='store_true')
    parser.add_argument('--profile', help='Aplicar un perfil local por su identificador')
    args = parser.parse_args()
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(directory / 'events.log', maxBytes=250_000, backupCount=2)
    (directory / 'events.log').chmod(0o600)
    logging.basicConfig(handlers=[handler], level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.diagnose:
        from app.audio.pipewire import PipeWire
        from app.controllers.shure import ShureController
        from app.diagnostics.report import report
        try:
            state, error = ShureController().read(), None
        except Exception as exc:
            state, error = None, str(exc)
        print(json.dumps(report(PipeWire(), state, error), ensure_ascii=False, indent=2))
        return
    from app.ui.application import ShureApplication
    return ShureApplication(screenshot=args.screenshot).run(['shure-control-linux', *sys.argv[1:]])


if __name__ == '__main__':
    sys.exit(main())
