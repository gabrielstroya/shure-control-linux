#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Native GTK acceptance with real MV7, explicit opt-in and isolated journals."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-device-changes', action='store_true', required=True)
    parser.parse_args()
    os.environ.setdefault('GSK_RENDERER', 'cairo')
    from gi.repository import GLib
    from app.ui.application import ShureApplication
    from app.controllers.runtime import ControllerRuntime
    from app.controllers.shure import ShureController
    from app.core import save_json
    from app.desktop.settings import DesktopSettings, Autostart
    from app.profiles.store import Profiles
    from app.audio.pipewire import PipeWire

    context = GLib.MainContext.default()
    results, errors = [], []
    restored = False

    def pump(seconds=0.1):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            while context.pending():
                context.iteration(False)
            time.sleep(0.01)

    with tempfile.TemporaryDirectory(prefix='shure-ui-acceptance-') as directory:
        directory = Path(directory)
        runtime = ControllerRuntime(
            shure=ShureController(state_path=directory / 'hardware-before.json'),
            audio=PipeWire(journal=directory / 'audio.json'),
            profiles=Profiles(directory / 'profiles.json'))
        runtime.preferences_path = directory / 'preferences.json'
        runtime.preferences = {}
        app = ShureApplication(runtime=runtime,
                               desktop_settings=DesktopSettings(directory / 'desktop.json'))
        app.autostart = Autostart(path=directory / 'autostart.desktop')
        app.set_application_id('io.github.shurecontrol.Acceptance')
        before, window = None, None
        executable = runtime.shure.executable

        def wait():
            end = time.monotonic() + 45
            pump()
            while runtime.busy and time.monotonic() < end:
                pump()
            assert not runtime.busy, 'GTK operation timed out'
            pump(.15)

        try:
            before = runtime.shure.read()
            app.register(None)
            assert not app.get_is_remote(), 'Another acceptance process is running'
            app.activate()
            window = app.window
            wait()
            # The shared runtime owns polling now; prevent test/reread races.
            GLib.source_remove(runtime.poll_id)
            runtime.poll_id = 0
            assert window.hardware and window.hardware['model'] == 'MV7'
            results.append('real_hardware_display')
            window.toggle_mute()
            wait()
            assert runtime.shure.read()['muted'] != before['muted']
            window.toggle_mute()
            wait()
            assert runtime.shure.read()['muted'] == before['muted']
            results.append('mute_buttons_and_readback')
            window.mode.set_selected(window.mode.options.index('manual'))
            target = before['settings']['gain_db'] - 1.5 if before['settings']['gain_db'] > 0 else 1.5
            window.gain.set_value(target)
            window.apply_main()
            wait()
            assert runtime.shure.read()['settings']['gain_db'] == target
            window.undo_hardware()
            wait()
            assert runtime.shure.read()['settings'] == before['settings']
            results.append('gain_button_and_undo')
            runtime.shure.executable = '/nonexistent/shurectl'
            window.refresh()
            wait()
            assert window.hardware is None and not window.mute_button.get_sensitive()
            runtime.shure.executable = executable
            window.refresh()
            wait()
            assert window.hardware and window.mute_button.get_sensitive()
            results.append('backend_failure_and_automatic_recovery_simulated')
            window.toggle_meter()
            pump(2)
            assert runtime.meter.process and not runtime.meter.error
            assert runtime.meter.visual_snapshot()['spectrum']
            results.append('real_input_meter_fft_and_waveform_in_window')
            evidence = ROOT / 'docs/evidence'
            evidence.mkdir(parents=True, exist_ok=True)
            for name in ('main', 'dsp', 'aec', 'profiles', 'diagnostics', 'preferences'):
                window.stack.set_visible_child_name(name)
                pump(.3)
                if name != 'main':
                    assert runtime.meter.process is None and window.meter_id == 0
                path = evidence / f'ui-{name}.png'
                window.screenshot_path = str(path)
                window.capture_screenshot()
                assert path.exists() and path.stat().st_size > 0
            results.append('six_native_pages_rendered_capture_stops_on_page_change')
            window.profile_combo.set_selected(window.profile_combo.options.index('whatsapp'))
            window.save_profile()
            wait()
            assert runtime.profiles.get('whatsapp')['customized']
            results.append('profile_save_via_ui')
            source, sink = window.selected_pair()
            runtime.audio.enable(source, sink, adopt=True)
            for key, mode, aec in [('teams', 'auto', True), ('recording', 'manual', False)]:
                window.profile_combo.set_selected(window.profile_combo.options.index(key))
                window.apply_profile()
                wait()
                assert runtime.shure.read()['settings']['mode'] == mode
                assert bool(runtime.audio.echo_modules(runtime.audio.snapshot())) == aec
            results.append('profile_apply_to_hardware_and_aec_via_ui')
        except Exception as exc:
            errors.append(f'{type(exc).__name__}: {exc}')
        finally:
            runtime.shure.executable = executable
            # Every restoration is attempted even if another one fails.
            try:
                if runtime.poll_id:
                    GLib.source_remove(runtime.poll_id)
                    runtime.poll_id = 0
                wait()
                runtime.stop_meter()
            except Exception as exc:
                errors.append(f'Worker shutdown: {exc}')
            hardware_restored, audio_restored = before is None, False
            try:
                if before is not None:
                    runtime.shure.restore(before)
                    after = runtime.shure.read()
                    hardware_restored = (after['settings'] == before['settings'] and
                                         after['muted'] == before['muted'])
                    assert hardware_restored, 'Hardware readback differs after restoration'
            except Exception as exc:
                errors.append(f'Hardware restoration: {exc}')
            try:
                if runtime.audio.journal.exists():
                    runtime.audio.restore()
                audio_restored = not runtime.audio.journal.exists()
            except Exception as exc:
                errors.append(f'Audio restoration: {exc}')
            restored = hardware_restored and audio_restored
            try:
                if window and not window.closed:
                    window.dispose_window()
                    window.destroy()
                    app.window = None
                if app.started:
                    app.finish_quit()
                elif not runtime.closed:
                    runtime.shutdown()
            except Exception as exc:
                errors.append(f'Application cleanup: {exc}')
            if not restored:
                recovery = ROOT / 'docs/evidence/ui-recovery'
                recovery.mkdir(parents=True, exist_ok=True, mode=0o700)
                import shutil
                for path in directory.glob('*.json'):
                    shutil.copy2(path, recovery / (path.stem + '.private.json'))
                    (recovery / (path.stem + '.private.json')).chmod(0o600)
                errors.append('Recovery journals preserved in docs/evidence/ui-recovery')
    report = {'passed': results, 'errors': errors, 'physical_unplug_tested': False, 'restored': restored}
    save_json(ROOT / 'docs/evidence/ui-test.json', report)
    print(json.dumps(report, indent=2))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
