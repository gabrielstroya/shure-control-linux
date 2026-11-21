#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Three real GTK processes verify AEC persistence without writing Shure settings.

Opt-in: --allow-audio-changes. Requires the current defaults to be the physical
MV7 input and HDMI output, with zero AEC modules. No PipeWire restart, microphone
capture, profile application, mute, gain, firmware or internal-DSP writes occur.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PHASES = ('keep_active', 'restore_on_exit', 'fresh_enable_disable')


def hardware_view(state):
    """Only values needed for a read-only before/after comparison; no USB serial."""
    return {key: state[key] for key in ('muted', 'settings')}


def audio_view(audio, data):
    return {
        'defaults': [data['default_source'], data['default_sink']],
        'aec': sorted([[module['name'], module.get('argument', '')]
                       for module in audio.echo_modules(data)]),
        'volumes': {kind + ':' + device['name']: [channel['value'] for channel in device.get('volume', {}).values()]
                    for kind in ('sources', 'sinks') for device in audio.physical(data, kind)},
    }


def worker(directory, phase):
    os.environ.setdefault('GSK_RENDERER', 'cairo')
    from gi.repository import GLib
    from app.audio.pipewire import PipeWire
    from app.controllers.runtime import ControllerRuntime
    from app.controllers.shure import ShureController
    from app.core import read_json, save_json
    from app.desktop.settings import DesktopSettings, Autostart
    from app.profiles.store import Profiles
    from app.ui.application import ShureApplication

    directory = Path(directory).resolve()
    assert directory.parent == Path('/tmp') and directory.name.startswith('shure-aec-restart-')
    baseline = read_json(directory / 'baseline.json')

    class ReadOnlyShure:
        """Deliberately expose no device mutation method to the real runtime/UI."""
        last_before = None

        def __init__(self):
            self._reader = ShureController(state_path=directory / 'never-written-hardware.json')

        def read(self):
            return self._reader.read()

        def devices(self):
            return self._reader.devices()

    audio = PipeWire(journal=directory / 'audio-session.json')
    runtime = ControllerRuntime(shure=ReadOnlyShure(), audio=audio,
                                profiles=Profiles(directory / 'profiles.json'))
    hardware_before = hardware_view(runtime.shure.read())
    runtime.preferences_path = directory / 'preferences.json'
    runtime.preferences = read_json(runtime.preferences_path)
    settings = DesktopSettings(directory / 'desktop.json')
    settings.update(resident_enabled=False, close_behavior='quit',
                    keep_aec_on_exit=phase == 'keep_active')
    app = ShureApplication(runtime=runtime, desktop_settings=settings)
    app.set_application_id(baseline['application_id'])
    app.autostart = Autostart(path=directory / 'never-created-autostart.desktop')
    context = GLib.MainContext.default()
    passed, errors = [], []
    attempted = False
    timer_id = 0

    def pump(seconds=.05):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            while context.pending():
                context.iteration(False)
            time.sleep(.01)

    def until(predicate, message, timeout=50):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            pump()
        assert predicate(), message

    def idle():
        pump()
        until(lambda: not runtime.busy, 'Controller operation timed out')
        pump()

    def assert_ui(active):
        window = app.window
        assert window and window.get_visible() and not window.closed
        assert runtime.hardware and runtime.hardware['model'] == 'MV7'
        assert runtime.hardware_error is None, 'MV7 read failed'
        assert runtime.audio_error is None, 'Current audio read/reconciliation failed'
        assert runtime.operation_error is None, 'An operation error remains visible'
        assert runtime.aec_state() == (('active', True) if active else ('off', False))
        expected = audio.status(runtime.audio_data)
        assert window.main_aec.get_subtitle() == expected
        assert window.aec_row.get_subtitle() == expected
        assert ('Activo en las rutas predeterminadas' if active else 'Desactivado') in expected
        assert window.default_row.get_subtitle() == runtime.audio_data['default_source']
        for message in (window.status.get_label(), runtime.last_message or '',
                        window.main_aec.get_subtitle(), window.aec_row.get_subtitle()):
            assert 'Los predeterminados cambiaron fuera' not in message, 'Obsolete defaults conflict remains'
        assert runtime.meter.process is None and runtime.meter.thread is None
        assert window.meter_id == 0 and not runtime.meter.visuals_enabled
        assert hardware_view(runtime.shure.read()) == hardware_before, 'Shure state changed externally'
        assert not (directory / 'never-written-hardware.json').exists()

    def toggle(expected):
        assert runtime.toggle_aec(), 'AEC action was rejected'
        idle()
        assert_ui(expected)
        assert len(audio.echo_modules(audio.snapshot())) == (1 if expected else 0)

    def execute_phase():
        nonlocal attempted
        if attempted:
            return False
        attempted = True
        try:
            until(lambda: app.window is not None and runtime.updated_at is not None and not runtime.busy,
                  'Fresh GTK window did not receive current state')
            # One serialized real refresh per transition, with no periodic race.
            if runtime.poll_id:
                GLib.source_remove(runtime.poll_id)
                runtime.poll_id = 0
            assert_ui(phase == 'restore_on_exit')
            passed.append('fresh_window_matches_current_audio_without_stale_notice')
            if phase == 'keep_active':
                assert (runtime.audio_reconciliation or {}).get('state') == 'archived'
                passed.append('fresh_gtk_start_archives_obsolete_virtual_route_journal')
            if phase == 'restore_on_exit':
                assert audio.journal.exists()
                assert (runtime.audio_reconciliation or {}).get('state') == 'current'
                passed.append('new_process_recovers_owned_active_aec')
            else:
                assert audio_view(audio, audio.snapshot()) == baseline['audio']
                toggle(True)
                passed.append('aec_enabled_via_shared_runtime')
                if phase == 'fresh_enable_disable':
                    toggle(False)
                    assert audio_view(audio, audio.snapshot()) == baseline['audio']
                    passed.append('aec_disabled_via_shared_runtime_returns_physical_defaults')
            app.request_quit()
            passed.append('quit_requested_through_application_lifecycle')
        except Exception as exc:
            errors.append(f'{type(exc).__name__}: {exc}')
            # Leave any owned audio for the supervisor's guarded cleanup. Never
            # bypass request_quit or force-stop the process/desktop services.
            app.settings = settings.update(keep_aec_on_exit=True)
            app.request_quit()
        return False

    def watchdog():
        errors.append('Application did not exit within its phase deadline')
        app.settings = settings.update(keep_aec_on_exit=True)
        if not app.quit_pending:
            app.request_quit()
        return False

    GLib.idle_add(execute_phase)
    timer_id = GLib.timeout_add_seconds(100, watchdog)
    returncode = app.run([str(Path(__file__).resolve())])
    if context.find_source_by_id(timer_id):
        GLib.source_remove(timer_id)
    try:
        assert attempted and returncode == 0
        assert runtime.closed and app.window is None and not app.resident
        assert runtime.meter.process is None
        data = audio.snapshot()
        if phase == 'keep_active' and not errors:
            record = read_json(audio.journal)
            assert record and len(audio.echo_modules(data)) == 1 and audio._active(record, data)
            assert record['expected_defaults'] == [data['default_source'], data['default_sink']]
            assert record['before']['default_source'] == baseline['audio']['defaults'][0]
            assert record['before']['default_sink'] == baseline['audio']['defaults'][1]
            passed.append('process_exited_owned_aec_still_active')
        elif not errors:
            assert audio_view(audio, data) == baseline['audio']
            assert not audio.journal.exists()
            passed.append('process_exited_physical_defaults_and_zero_aec')
        assert hardware_view(runtime.shure.read()) == hardware_before
        assert not (directory / 'never-written-hardware.json').exists()
        passed.append('shure_state_unchanged_read_only')
    except Exception as exc:
        errors.append(f'Post-exit verification: {type(exc).__name__}: {exc}')
    report = {'phase': phase, 'pid': os.getpid(), 'passed': passed, 'errors': errors,
              'hardware_writes_permitted': False, 'capture_started': False}
    save_json(directory / (phase + '.json'), report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 1 if errors else 0


def supervise():
    from app.audio.pipewire import PipeWire
    from app.controllers.shure import ShureController
    from app.core import read_json, save_json

    directory = Path(tempfile.mkdtemp(prefix='shure-aec-restart-', dir='/tmp'))
    directory.chmod(0o700)
    audio = PipeWire(journal=directory / 'audio-session.json')
    shure = ShureController(state_path=directory / 'never-written-hardware.json')
    baseline, reports, errors = None, [], []
    hardware_before = None
    restored, child_running = False, False
    try:
        data = audio.snapshot()
        source, sink = data['default_source'], data['default_sink']
        audio.validate_pair(source, sink, data)
        assert 'shure' in source.lower(), 'The current physical default input must be the Shure MV7'
        assert 'hdmi' in sink.lower(), 'The current physical default output must be HDMI'
        assert not audio.echo_modules(data), 'Baseline must contain zero AEC modules; no external AEC will be adopted'
        hardware_before = hardware_view(shure.read())
        baseline = {'audio': audio_view(audio, data),
                    'application_id': 'io.github.shurecontrol.AecRestartAcceptance.p' + str(os.getpid())}
        save_json(directory / 'baseline.json', baseline)
        save_json(directory / 'preferences.json', {'source': source, 'sink': sink})
        # Historical routes are a fixture in this private journal only. The
        # first real GTK startup must retire them without reviving that module.
        save_json(audio.journal, {
            'version': 1,
            'before': {
                'default_source': 'shure_sin_eco', 'default_sink': 'altavoces_sin_eco',
                'modules': [{
                    'name': 'module-echo-cancel', 'index': 536870916,
                    'argument': ('source_name=shure_sin_eco sink_name=altavoces_sin_eco '
                                 f'source_master={source} sink_master={sink} aec_method=webrtc'),
                }],
            },
            'active': None, 'moves': [],
            'expected_defaults': ['shure_sin_eco', 'altavoces_sin_eco'],
        })
        for phase in PHASES:
            print('Starting real GTK phase: ' + phase, flush=True)
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                      '--allow-audio-changes', '--worker', phase,
                                      '--directory', str(directory)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     cwd=ROOT, env={**os.environ, 'GSK_RENDERER': 'cairo'})
            try:
                output, diagnostic = child.communicate(timeout=145)
            except subprocess.TimeoutExpired:
                child_running = True
                # Never kill a GTK/controller process in the middle of restoration.
                errors.append(f'GTK phase {phase} remains running (PID {child.pid}); close its window normally. Recovery retained.')
                break
            report = read_json(directory / (phase + '.json'), None)
            if report is not None:
                reports.append(report)
            if child.returncode != 0 or report is None:
                if diagnostic:
                    print(diagnostic[-4000:], file=sys.stderr, flush=True)
                if report is None:
                    errors.append('Phase ' + phase + ' failed before writing its report: ' + diagnostic[-1200:])
                else:
                    errors.extend(phase + ': ' + error for error in report['errors'])
                break
            print('Passed: ' + phase, flush=True)
        if not child_running and not errors:
            assert len(reports) == 3 and len({report['pid'] for report in reports}) == 3
            assert hardware_view(shure.read()) == hardware_before, 'Shure settings differ; no hardware restoration attempted'
            restored = audio_view(audio, audio.snapshot()) == baseline['audio']
            assert restored, 'Final audio state differs from the physical baseline'
            assert not audio.journal.exists()
    except Exception as exc:
        errors.append(f'{type(exc).__name__}: {exc}')
    finally:
        if errors and baseline and not child_running:
            try:
                data = audio.snapshot()
                modules = audio.echo_modules(data)
                record = read_json(audio.journal)
                if record:
                    assert record['before']['modules'] == [], 'Cleanup refuses a journal with external AEC baseline'
                    assert [record['before']['default_source'], record['before']['default_sink']] == baseline['audio']['defaults']
                    owned = audio._active(record, data)
                    assert ((len(modules) == 1 and owned and audio._same(modules[0], owned)) or
                            (not modules and [data['default_source'], data['default_sink']] == baseline['audio']['defaults'])), 'Cleanup refused: current AEC/routes are not confirmed as this test session'
                    audio.restore()
                restored = audio_view(audio, audio.snapshot()) == baseline['audio']
                assert restored, 'Audio cleanup did not restore the baseline'
                assert hardware_view(shure.read()) == hardware_before, 'Shure changed externally; read-only test does not restore it'
            except Exception as exc:
                errors.append(f'Guarded audio cleanup: {type(exc).__name__}: {exc}')
        report = {'phases': reports, 'errors': errors, 'audio_restored': restored,
                  'three_distinct_processes': len(reports) == 3 and len({r['pid'] for r in reports}) == 3,
                  'hardware_writes_permitted': False, 'pipewire_restarted': False,
                  'capture_started': False, 'acoustic_efficacy_tested': False,
                  'recovery_directory': str(directory) if errors else None}
        save_json(ROOT / 'docs/evidence/aec-reconciliation/restart-live.json', report)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if not errors:
            shutil.rmtree(directory)
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-audio-changes', action='store_true', required=True)
    parser.add_argument('--worker', choices=PHASES, help=argparse.SUPPRESS)
    parser.add_argument('--directory', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Ejecuta esta prueba como tu usuario habitual, sin sudo ni root.')
    if args.worker:
        if not args.directory:
            parser.error('--directory is required for a worker')
        return worker(args.directory, args.worker)
    return supervise()


if __name__ == '__main__':
    raise SystemExit(main())
