#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Opt-in GTK/GNOME, DBus-menu and real-MV7 resident acceptance.

Uses private temporary preferences/journals; restores audio and hardware in finally.
No desktop extension, autostart entry or global shortcut is installed.
"""
import argparse
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
APP_ID = 'io.github.shurecontrol.ResidentAcceptance'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-device-changes', action='store_true', required=True)
    parser.parse_args()
    os.environ.setdefault('GSK_RENDERER', 'cairo')
    from gi.repository import Gio, GLib
    from app.audio.pipewire import PipeWire
    from app.controllers.runtime import ControllerRuntime
    from app.controllers.shure import ShureController
    from app.core import save_json
    from app.desktop.indicator import MENU_PATH, MENU_IFACE, WATCHER, WATCHER_PATH
    from app.desktop.settings import DesktopSettings, Autostart
    from app.profiles.store import Profiles
    from app.ui.application import ShureApplication

    context = GLib.MainContext.default()
    results, errors, metrics = [], [], {}
    restored, host_available = False, False

    def pump(seconds=.1):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            while context.pending():
                context.iteration(False)
            time.sleep(.01)

    def until(predicate, message, timeout=45):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            pump()
        assert predicate(), message

    def fingerprint(audio, data):
        return {'defaults': (data['default_source'], data['default_sink']),
                'aec': sorted((m['name'], m.get('argument', '')) for m in audio.echo_modules(data)),
                'volumes': {kind + ':' + d['name']: tuple(v['value'] for v in d.get('volume', {}).values())
                            for kind in ('sources', 'sinks') for d in audio.physical(data, kind)}}

    with tempfile.TemporaryDirectory(prefix='shure-resident-acceptance-') as directory:
        directory = Path(directory)
        runtime = ControllerRuntime(
            shure=ShureController(state_path=directory / 'hardware-before.json'),
            audio=PipeWire(journal=directory / 'audio.json'),
            profiles=Profiles(directory / 'profiles.json'))
        runtime.preferences_path, runtime.preferences = directory / 'preferences.json', {}
        settings = DesktopSettings(directory / 'desktop.json')
        settings.update(resident_enabled=True, close_behavior='hide', keep_aec_on_exit=True)
        app = ShureApplication(runtime=runtime, desktop_settings=settings)
        app.set_application_id(APP_ID)
        app.autostart = Autostart(path=directory / 'autostart.desktop')
        before, audio_before, secondary = None, None, None
        client = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        def idle():
            pump(.1)
            until(lambda: not runtime.busy, 'Controller worker timed out')
            pump(.1)

        def refresh():
            idle()
            runtime.refresh()
            idle()

        def dbus(destination, path, interface, method, parameters):
            response, failures = [], []
            def finished(connection, result):
                try:
                    response.append(connection.call_finish(result))
                except Exception as exc:
                    failures.append(exc)
            client.call(destination, path, interface, method, parameters, None,
                        Gio.DBusCallFlags.NONE, 10000, None, finished)
            until(lambda: bool(response or failures), 'DBus request timed out', 12)
            if failures:
                raise failures[0]
            return response[0].unpack()

        def event(item_id):
            idle()
            name = app.indicator._connection.get_unique_name()
            dbus(name, MENU_PATH, MENU_IFACE, 'Event',
                 GLib.Variant('(isvu)', (item_id, 'clicked', GLib.Variant('s', ''), 0)))
            idle()

        def profile_item(key):
            return next(item.id for item in app.indicator.model.items.values()
                        if item.action == 'profile' and item.value == key)

        try:
            before = runtime.shure.read()
            # Full restoration of inactive DSP values cannot be asserted if the
            # original mode is auto; choose a manual baseline before this test.
            assert before['settings']['mode'] == 'manual', 'Resident acceptance requires a manual-mode baseline'
            audio_before = fingerprint(runtime.audio, runtime.audio.snapshot())
            app.register(None)
            assert not app.get_is_remote(), 'Another resident acceptance process is running'
            app.activate()
            idle()
            assert app.window and runtime.hardware and runtime.hardware['model'] == 'MV7'
            pump(1.5)
            host_available = app.indicator_available
            assert app.resident and app.indicator._connection
            name = app.indicator._connection.get_unique_name()
            layout = dbus(name, MENU_PATH, MENU_IFACE, 'GetLayout', GLib.Variant('(iias)', (0, -1, [])))
            children = layout[1][2]
            ids = {child[0] for child in children}
            assert {3, 10, 20, 30, 41, 51, 53} <= ids
            results.append('real_dbus_menu_layout')
            if host_available:
                registered = dbus(WATCHER, WATCHER_PATH, 'org.freedesktop.DBus.Properties', 'Get',
                                  GLib.Variant('(ss)', (WATCHER, 'RegisteredStatusNotifierItems')))[0]
                assert any(name in item for item in registered)
                results.append('gnome_host_registered_indicator')
            else:
                app.background_window(app.window)
                pump(.8)
                assert app.window.get_visible()
                assert app.window.stack.get_visible_child_name() == 'preferences'
                results.append('unsupported_indicator_keeps_window_accessible')

            # Temporarily pause periodic reads while testing serialized mutations.
            GLib.source_remove(runtime.poll_id)
            runtime.poll_id = 0
            event(3)
            assert runtime.shure.read()['muted'] != before['muted']
            event(3)
            assert runtime.shure.read()['muted'] == before['muted']
            results.append('mute_via_dbus_menu_and_readback')
            gain_before = runtime.shure.read()['settings']['gain_db']
            increase = gain_before < 36
            event(12 if increase else 11)
            assert runtime.shure.read()['settings']['gain_db'] == gain_before + (1.5 if increase else -1.5)
            event(11 if increase else 12)
            assert runtime.shure.read()['settings']['gain_db'] == gain_before
            results.append('gain_via_dbus_menu_and_readback')
            source, sink = app.window.selected_pair()
            runtime.save_pair(source, sink)
            # Explicit opt-in includes adopting/restoring a pre-existing AEC for
            # this acceptance only. User journals and existing clients are retained.
            runtime.audio.enable(source, sink, adopt=True)
            refresh()
            assert runtime.aec_state() == ('active', True)
            event(20)
            assert not runtime.audio.echo_modules(runtime.audio.snapshot())
            event(20)
            assert len(runtime.audio.echo_modules(runtime.audio.snapshot())) == 1
            results.append('aec_menu_state_disable_enable_without_duplicate')
            event(profile_item('recording'))
            assert runtime.shure.read()['settings']['mode'] == 'manual'
            assert not runtime.audio.echo_modules(runtime.audio.snapshot())
            event(profile_item('teams'))
            assert runtime.shure.read()['settings']['mode'] == 'auto'
            assert len(runtime.audio.echo_modules(runtime.audio.snapshot())) == 1
            runtime.profiles.save('custom', before, source, sink, False)
            refresh()
            for key, mode, aec in [('video', 'auto', True), ('narration', 'manual', False),
                                   ('elevenlabs', 'manual', False), ('custom', 'manual', False)]:
                event(profile_item(key))
                assert runtime.shure.read()['settings']['mode'] == mode
                assert bool(runtime.audio.echo_modules(runtime.audio.snapshot())) == aec
            results.append('all_requested_profiles_via_dbus_menu')

            window = app.show_window('main')
            window.toggle_meter()
            pump(1)
            assert runtime.meter.process and not runtime.meter.error
            assert runtime.meter.visual_snapshot()['spectrum']
            capture = runtime.meter.process
            window.stack.set_visible_child_name('dsp')
            pump(.2)
            assert runtime.meter.process is None and capture.poll() is not None
            assert window.meter_id == 0 and not runtime.meter.visuals_enabled
            results.append('page_change_stops_capture_fft_and_timer')
            if host_available:
                window.stack.set_visible_child_name('main')
                window.toggle_meter()
                pump(.3)
                capture = runtime.meter.process
                app.close_window(window)
                pump(.4)
                assert not window.get_visible() and app.resident
                assert capture.poll() is not None and runtime.meter.process is None
                assert window.meter_id == 0 and not runtime.meter.visuals_enabled
                results.append('close_to_indicator_stops_capture')
                event(41)
                assert runtime.meter.process and not runtime.meter.visuals_enabled
                until(lambda: runtime.meter.process is None, 'Quick meter did not stop automatically', 8)
                assert runtime.measure_timer == 0
                results.append('explicit_quick_measurement_stops_automatically_without_fft')
                # Measure a true resident interval, including periodic USB/audio reads.
                runtime.start()
                idle()
                start_wall = time.monotonic()
                start_cpu = time.process_time()
                start_children = resource.getrusage(resource.RUSAGE_CHILDREN)
                pump(6)
                end_children = resource.getrusage(resource.RUSAGE_CHILDREN)
                metrics['resident_wall_seconds'] = round(time.monotonic() - start_wall, 3)
                metrics['resident_cpu_seconds'] = round(time.process_time() - start_cpu, 4)
                metrics['resident_child_cpu_seconds'] = round(
                    end_children.ru_utime + end_children.ru_stime -
                    start_children.ru_utime - start_children.ru_stime, 4)
                metrics['resident_current_rss_kib'] = int(next(
                    line.split()[1] for line in Path('/proc/self/status').read_text().splitlines()
                    if line.startswith('VmRSS:')))
                metrics['process_peak_rss_kib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                assert runtime.meter.process is None and runtime.meter.thread is None
                assert not runtime.meter.visual_snapshot()['waveform']
                results.append('resident_idle_without_capture_or_visual_buffers')
                idle()
                GLib.source_remove(runtime.poll_id)
                runtime.poll_id = 0
                event(51)
                assert app.window is window and window.get_visible()
                assert runtime.meter.process is None
                app.settings = settings.update(close_behavior='resident')
                app.close_window(window)
                pump(.4)
                assert app.window is None and window.closed
                event(51)
                assert app.window is not window and app.window.runtime is runtime
                assert len(app.get_windows()) == 1 and runtime.meter.process is None
                results.append('destroy_reopen_reuses_controller_without_capture')
                # A second process registers as remote and activates this instance.
                snippet = (
                    'from gi.repository import Gio; '
                    f'a=Gio.Application(application_id={APP_ID!r}, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE); '
                    'a.register(None); print(a.get_is_remote()); a.activate()')
                secondary = subprocess.Popen([sys.executable, '-c', snippet], stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, text=True)
                until(lambda: secondary.poll() is not None, 'Remote activation timed out', 12)
                output, _diagnostic = secondary.communicate()
                assert secondary.returncode == 0 and output.strip() == 'True'
                assert len(app.get_windows()) == 1 and app.window.runtime is runtime
                results.append('second_process_forwards_to_existing_application')
            assert settings.load()['resident_enabled']
            assert not app.autostart.path.exists()
            results.append('isolated_preferences_persist_without_enabling_autostart')
        except Exception as exc:
            errors.append(f'{type(exc).__name__}: {exc}')
        finally:
            if secondary and secondary.poll() is None:
                secondary.terminate()
                secondary.communicate(timeout=5)
            try:
                if runtime.poll_id:
                    GLib.source_remove(runtime.poll_id)
                    runtime.poll_id = 0
                idle()
                runtime.stop_meter()
            except Exception as exc:
                errors.append(f'Worker cleanup: {exc}')
            hardware_restored, audio_restored = before is None, audio_before is None
            try:
                if before is not None:
                    runtime.shure.restore(before)
                    after = runtime.shure.read()
                    hardware_restored = after['settings'] == before['settings'] and after['muted'] == before['muted']
                    assert hardware_restored, 'Hardware differs after restoration'
            except Exception as exc:
                errors.append(f'Hardware restoration: {exc}')
            try:
                if runtime.audio.journal.exists():
                    runtime.audio.restore()
                if audio_before is not None:
                    audio_restored = fingerprint(runtime.audio, runtime.audio.snapshot()) == audio_before
                    assert audio_restored, 'Audio defaults, modules or physical volumes differ after restoration'
            except Exception as exc:
                errors.append(f'Audio restoration: {exc}')
            restored = hardware_restored and audio_restored
            try:
                if app.started:
                    # Exercise the actual quit command after original audio was restored.
                    app.request_quit()
                    until(lambda: runtime.closed, 'Resident did not quit completely', 10)
                    assert not app.resident and app.window is None
                    assert runtime.meter.process is None and app.indicator._connection is None
                    results.append('full_quit_removes_indicator_capture_and_controller')
                else:
                    runtime.shutdown()
            except Exception as exc:
                errors.append(f'Application cleanup: {exc}')
            if not restored:
                recovery = ROOT / 'docs/evidence/resident-recovery'
                recovery.mkdir(parents=True, exist_ok=True, mode=0o700)
                for path in directory.glob('*.json'):
                    shutil.copy2(path, recovery / (path.stem + '.private.json'))
                    (recovery / (path.stem + '.private.json')).chmod(0o600)
                errors.append('Recovery journals preserved in docs/evidence/resident-recovery')
    report = {'passed': results, 'errors': errors, 'restored': restored,
              'gnome_indicator_host_registered': host_available, 'metrics': metrics,
              'physical_unplug_tested': False, 'session_restart_tested': False,
              'acoustic_echo_efficacy_tested': False}
    save_json(ROOT / 'docs/evidence/resident-test.json', report)
    print(json.dumps(report, indent=2))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
