#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Render the actual GTK UI with public demo data; no HID/PipeWire/capture I/O."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('GSK_RENDERER', 'cairo')
os.environ.setdefault('GSETTINGS_BACKEND', 'memory')


def main():
    with tempfile.TemporaryDirectory(prefix='shure-screenshots-') as temporary:
        os.environ['XDG_CONFIG_HOME'] = temporary + '/config'
        os.environ['XDG_STATE_HOME'] = temporary + '/state'
        from gi.repository import GLib
        from app.audio.pipewire import PipeWire
        from app.controllers.runtime import ControllerRuntime
        from app.desktop.settings import DesktopSettings, Autostart
        from app.ui.application import ShureApplication

        hardware = {'model': 'MV7', 'firmware_version': 'demo', 'muted': False,
                    'settings': {'mode': 'manual', 'gain_db': 24.0, 'monitor_mix': 50,
                                 'eq_preset': 'flat', 'compressor': 'off', 'limiter_enabled': False,
                                 'auto_position': 'near', 'auto_tone': 'natural'}}
        source, sink = 'alsa_input.Shure_MV7.demo', 'alsa_output.HDMI.demo'
        data = {'default_source': source, 'default_sink': sink, 'modules': [],
                'source-outputs': [], 'sink-inputs': [],
                'sources': [{'index': 1, 'name': source, 'description': 'Shure MV7 Mono',
                             'sample_specification': 's24le 1ch 48000Hz',
                             'volume': {'mono': {'value': 65536, 'value_percent': '100%'}}}],
                'sinks': [{'index': 2, 'name': sink, 'description': 'HDMI Stereo',
                           'sample_specification': 's32le 2ch 48000Hz',
                           'volume': {'front-left': {'value': 32768, 'value_percent': '50%'}}}]}

        class DemoShure:
            last_before = None

            def read(self):
                return copy.deepcopy(hardware)

            def devices(self):
                return []

        class DemoAudio(PipeWire):
            def snapshot(self):
                return copy.deepcopy(data)

            def command(self, *args):
                raise AssertionError('Documentation screenshots cannot access real audio')

        runtime = ControllerRuntime(shure=DemoShure(), audio=DemoAudio(journal=Path(temporary)/'audio.json'))
        runtime.preferences = {'source': source, 'sink': sink}
        settings = DesktopSettings(Path(temporary)/'desktop.json')
        settings.update(resident_enabled=False, keep_aec_on_exit=True, close_behavior='quit')
        app = ShureApplication(runtime=runtime, desktop_settings=settings)
        app.set_application_id('io.github.shurecontrol.Documentation')
        app.autostart = Autostart(Path(temporary)/'autostart.desktop')
        directory = ROOT/'docs/screenshots'
        directory.mkdir(parents=True, exist_ok=True)
        pages = iter([('main', 'microphone'), ('aec', 'echo-cancellation'), ('preferences', 'preferences')])
        failures = []
        deadline = time.monotonic() + 30

        def capture_next():
            try:
                if runtime.updated_at is None or runtime.busy:
                    if time.monotonic() > deadline:
                        raise RuntimeError('Demo runtime did not become ready')
                    return True
                page, name = next(pages)
                app.window.stack.set_visible_child_name(page)
                runtime.last_message = 'Demostración · sin acceso USB ni captura de audio'
                app.window.status.set_label(runtime.last_message)
                # The real widget renderer provides unedited screenshots.
                def capture():
                    app.window.screenshot_path = str(directory/(name+'.png'))
                    app.window.capture_screenshot()
                    if not (directory/(name+'.png')).is_file():
                        failures.append(name)
                    GLib.timeout_add(150, capture_next)
                    return False
                GLib.timeout_add(350, capture)
                return False
            except StopIteration:
                app.request_quit()
                return False
            except Exception as exc:
                failures.append(str(exc))
                app.request_quit()
                return False

        with patch('app.ui.application.PortalShortcuts', return_value=Mock()):
            GLib.timeout_add(150, capture_next)
            code = app.run(['shure-documentation'])
        if code or failures:
            raise SystemExit('Screenshot rendering failed: ' + ', '.join(failures))
        print('Rendered 3 GTK screenshots with demo data; no hardware or capture access.')


if __name__ == '__main__':
    main()
