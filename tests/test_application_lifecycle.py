# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Application policy tested without GTK windows, D-Bus, devices or audio I/O."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.ui import application
from app.ui.application import ShureApplication


class Scheduler:
    def __init__(self):
        self.callbacks = {}
        self.next_id = 1

    def add(self, milliseconds, callback):
        identifier = self.next_id
        self.next_id += 1
        self.callbacks[identifier] = (milliseconds, callback)
        return identifier

    def remove(self, identifier):
        self.callbacks.pop(identifier, None)

    def run(self, identifier):
        _interval, callback = self.callbacks[identifier]
        keep = callback()
        if not keep:
            self.remove(identifier)
        return keep

    def one(self, milliseconds):
        matches = [identifier for identifier, (interval, _callback) in self.callbacks.items()
                   if interval == milliseconds]
        if len(matches) != 1:
            raise AssertionError(f'Se esperaba un temporizador de {milliseconds} ms; hay {matches}')
        return matches[0]


class Runtime:
    def __init__(self):
        self.busy = False
        self.closed = False
        self.listeners, self.message_listeners = [], []
        self.audio = SimpleNamespace(journal=Mock(), restore=Mock())
        self.audio.journal.exists.return_value = True
        self.start = Mock()
        self.refresh = Mock()
        self.shutdown = Mock(side_effect=self._shutdown)
        self.hardware = {'model': 'MV7', 'serial': 'private-test-value', 'muted': False}
        self.snapshot = Mock(side_effect=lambda resident: {'hardware': self.hardware, 'resident': resident})
        self.work_calls = []
        self.pending_work = None

    def work(self, message, task, on_result=None):
        self.work_calls.append(message)
        self.pending_work = (task, on_result)
        self.busy = True
        return True

    def complete_work(self):
        task, on_result = self.pending_work
        self.pending_work = None
        self.busy = False
        try:
            result = task()
        except RuntimeError:
            return False
        if on_result:
            on_result(result)
        return True

    def _shutdown(self):
        self.closed = True


class ApplicationHarness:
    """Bind production policy methods to a plain Python object, not GApplication."""
    on_command_line = ShureApplication.on_command_line
    request_quit = ShureApplication.request_quit
    finish_quit = ShureApplication.finish_quit

    def __init__(self, *, started=False, resident=False, keep_aec=False):
        self.started = started
        self.resident = resident
        self.settings = {'resident_enabled': True, 'keep_aec_on_exit': keep_aec}
        self.runtime = Runtime()
        self.quitting = self.quit_pending = self.quit_hold = False
        self.holds = int(resident)
        self.window = None
        self.indicator = Mock()
        self.shortcuts = Mock()
        self.signal_ids = []
        self.withdraw_notification = Mock()
        self.quit = Mock()
        self.show_window = Mock()
        self.set_resident = Mock()
        self.ensure_started = Mock(side_effect=self._start)
        self.on_state = Mock()
        self.on_message = Mock()
        self.perform = Mock()
        self.on_indicator_availability = Mock()
        self.on_shortcut_status = Mock()
        self.signal_quit = Mock()

    def _start(self, allow_resident=True):
        if not self.started:
            self.started = True
            if allow_resident and self.settings['resident_enabled']:
                self.resident = True
                self.hold()

    def hold(self):
        self.holds += 1

    def release(self):
        self.holds -= 1
        if self.holds < 0:
            raise AssertionError('release sin hold correspondiente')


class ApplicationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = Scheduler()
        timer = patch.object(application.GLib, 'timeout_add', side_effect=self.scheduler.add)
        remove = patch.object(application.GLib, 'source_remove', side_effect=self.scheduler.remove)
        timer.start()
        remove.start()
        self.addCleanup(timer.stop)
        self.addCleanup(remove.stop)

    @staticmethod
    def command(*args):
        return SimpleNamespace(get_arguments=lambda: ['shure-control-linux', *args],
                               print_literal=Mock(), printerr_literal=Mock())

    def test_restore_cli_entry_uses_shared_worker_and_ephemeral_success_restores_once(self):
        from app import main as entrypoint
        app = ApplicationHarness(keep_aec=False)
        command = self.command('--restore-audio')
        context = Mock()

        def complete(_blocking):
            # Nothing reaches PipeWire before the shared serialized work runs.
            app.runtime.audio.restore.assert_not_called()
            self.assertEqual(app.runtime.work_calls, ['Audio restaurado'])
            app.runtime.complete_work()

        context.iteration.side_effect = complete
        entry_application = Mock()
        entry_application.run.side_effect = lambda _args: app.on_command_line(None, command)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'events.log').touch()
            with patch.object(entrypoint, 'state_dir', return_value=path), \
                 patch.object(entrypoint, 'RotatingFileHandler', return_value=Mock()), \
                 patch.object(entrypoint.logging, 'basicConfig'), \
                 patch.object(entrypoint.sys, 'argv', ['shure-control-linux', '--restore-audio']), \
                 patch.object(application, 'ShureApplication', return_value=entry_application), \
                 patch('app.audio.pipewire.PipeWire') as direct_audio, \
                 patch.object(application.GLib.MainContext, 'default', return_value=context):
                result = entrypoint.main()
                direct_audio.assert_not_called()
        self.assertEqual(result, 0)
        entry_application.run.assert_called_once_with(['shure-control-linux', '--restore-audio'])
        app.ensure_started.assert_called_once_with(allow_resident=False)
        app.runtime.audio.restore.assert_called_once()
        app.runtime.shutdown.assert_called_once()
        command.print_literal.assert_called_once_with('Configuración de audio restaurada.\n')
        command.printerr_literal.assert_not_called()
        self.assertFalse(app.resident)
        self.assertTrue(app.quitting)
        self.assertEqual(app.holds, 0)

    def test_restore_cli_failure_reports_nonzero_without_exit_policy_retry(self):
        app = ApplicationHarness(keep_aec=False)
        app.runtime.audio.restore.side_effect = RuntimeError('Las rutas cambiaron externamente')
        command = self.command('--restore-audio')
        context = Mock()
        context.iteration.side_effect = lambda _blocking: app.runtime.complete_work()
        with patch.object(application.GLib.MainContext, 'default', return_value=context):
            result = app.on_command_line(None, command)
        self.assertEqual(result, 1)
        app.ensure_started.assert_called_once_with(allow_resident=False)
        app.runtime.audio.restore.assert_called_once()
        command.print_literal.assert_not_called()
        command.printerr_literal.assert_called_once_with('Las rutas cambiaron externamente\n')
        self.assertEqual(len(app.runtime.work_calls), 1)
        self.assertEqual(self.scheduler.callbacks, {})
        self.assertTrue(app.quitting)
        self.assertFalse(app.resident)
        self.assertEqual(app.holds, 0)

    def test_restore_cli_busy_rejection_does_not_bypass_worker_or_close_resident(self):
        app = ApplicationHarness(started=True, resident=True, keep_aec=False)
        app.runtime.busy = True
        app.runtime.work = Mock(return_value=False)
        command = self.command('--restore-audio')
        context = Mock()
        with patch.object(application.GLib.MainContext, 'default', return_value=context):
            result = app.on_command_line(None, command)
        self.assertEqual(result, 1)
        app.runtime.work.assert_called_once()
        app.runtime.audio.restore.assert_not_called()
        context.iteration.assert_not_called()
        command.print_literal.assert_not_called()
        command.printerr_literal.assert_called_once_with('Hay otra operación en curso.\n')
        app.runtime.shutdown.assert_not_called()
        self.assertTrue(app.runtime.busy)
        self.assertTrue(app.resident)
        self.assertFalse(app.quitting)
        self.assertEqual(app.holds, 1)

    def test_status_without_instance_cannot_apply_exit_audio_policy_or_resident_preference(self):
        app = ApplicationHarness(keep_aec=False)
        command = self.command('--status')
        context = Mock()
        with patch.object(application.GLib.MainContext, 'default', return_value=context):
            result = app.on_command_line(None, command)
        self.assertEqual(result, 0)
        app.ensure_started.assert_called_once_with(allow_resident=False)
        app.runtime.audio.restore.assert_not_called()
        self.assertEqual(app.runtime.work_calls, [])
        self.assertFalse(app.resident)
        self.assertEqual(app.holds, 0)
        self.assertTrue(app.quitting)
        app.runtime.shutdown.assert_called_once()
        output = json.loads(command.print_literal.call_args.args[0])
        self.assertNotIn('serial', output['hardware'])
        self.assertIn('serial', app.runtime.hardware)

    def test_status_existing_resident_remains_running_and_waits_for_fresh_read(self):
        app = ApplicationHarness(started=True, resident=True, keep_aec=False)
        app.runtime.refresh.side_effect = lambda: setattr(app.runtime, 'busy', True)
        context = Mock()
        context.iteration.side_effect = lambda _blocking: setattr(app.runtime, 'busy', False)
        with patch.object(application.GLib.MainContext, 'default', return_value=context):
            app.on_command_line(None, self.command('--status'))
        context.iteration.assert_called_once_with(True)
        app.runtime.audio.restore.assert_not_called()
        app.runtime.shutdown.assert_not_called()
        app.quit.assert_not_called()
        self.assertTrue(app.resident)
        self.assertEqual(app.holds, 1)

    def test_quit_without_instance_does_not_initialize_controller_or_touch_audio(self):
        app = ApplicationHarness()
        self.assertEqual(app.on_command_line(None, self.command('--quit')), 0)
        app.ensure_started.assert_not_called()
        app.runtime.start.assert_not_called()
        app.runtime.audio.restore.assert_not_called()
        app.runtime.shutdown.assert_not_called()
        self.assertEqual(self.scheduler.callbacks, {})
        self.assertEqual(app.holds, 0)

    def test_read_only_start_does_not_enable_saved_resident_preference(self):
        app = ApplicationHarness()
        with patch.object(application.Adw.StyleManager, 'get_default', return_value=Mock()), \
             patch.object(application, 'Indicator', return_value=Mock()), \
             patch.object(application, 'PortalShortcuts', return_value=Mock()), \
             patch.object(application.GLib, 'unix_signal_add', return_value=42):
            ShureApplication.ensure_started(app, allow_resident=False)
        self.assertTrue(app.started)
        app.runtime.start.assert_called_once()
        app.set_resident.assert_not_called()
        app.runtime.audio.restore.assert_not_called()
        self.assertEqual(app.holds, 0)

    def test_exit_waits_for_worker_and_ignores_duplicate_pending_requests(self):
        app = ApplicationHarness(started=True, keep_aec=True)
        app.runtime.busy = True
        app.request_quit()
        app.request_quit()
        timer = self.scheduler.one(100)
        self.assertTrue(self.scheduler.run(timer))
        self.assertTrue(app.quit_pending)
        self.assertEqual(app.holds, 1)
        app.runtime.shutdown.assert_not_called()
        app.runtime.busy = False
        self.assertFalse(self.scheduler.run(timer))
        self.assertTrue(app.quitting)
        self.assertEqual(app.holds, 0)
        app.runtime.audio.restore.assert_not_called()
        app.runtime.shutdown.assert_called_once()

    def test_restore_succeeds_before_controller_and_sessions_stop(self):
        app = ApplicationHarness(started=True, resident=True, keep_aec=False)
        app.request_quit()
        self.scheduler.run(self.scheduler.one(100))
        self.assertTrue(app.runtime.busy)
        self.assertEqual(app.holds, 2)
        app.runtime.shutdown.assert_not_called()
        self.assertTrue(app.runtime.complete_work())
        self.assertTrue(app.quitting)
        self.assertEqual(app.holds, 0)
        app.runtime.audio.restore.assert_called_once()
        app.runtime.shutdown.assert_called_once()
        app.indicator.stop.assert_called_once()
        app.shortcuts.stop.assert_called_once()
        self.assertFalse(self.scheduler.run(self.scheduler.one(150)))
        app.show_window.assert_not_called()

    def test_second_quit_during_restoration_does_not_queue_another_close(self):
        app = ApplicationHarness(started=True, resident=True, keep_aec=False)
        app.request_quit()
        self.scheduler.run(self.scheduler.one(100))
        self.assertTrue(app.runtime.busy)
        callbacks = dict(self.scheduler.callbacks)
        app.request_quit()
        self.assertEqual(self.scheduler.callbacks, callbacks)
        self.assertEqual(app.holds, 2)
        self.assertTrue(app.runtime.complete_work())
        self.assertEqual(app.holds, 0)
        self.assertEqual(len(app.runtime.work_calls), 1)

    def test_failed_restore_keeps_window_accessible_and_allows_explicit_retry(self):
        app = ApplicationHarness(started=True, resident=True, keep_aec=False)
        app.runtime.audio.restore.side_effect = RuntimeError('Las rutas cambiaron externamente')
        app.request_quit()
        self.scheduler.run(self.scheduler.one(100))
        self.assertFalse(app.runtime.complete_work())
        self.assertFalse(self.scheduler.run(self.scheduler.one(150)))
        app.show_window.assert_called_once_with('aec')
        app.runtime.shutdown.assert_not_called()
        app.quit.assert_not_called()
        self.assertFalse(app.quitting)
        self.assertFalse(app.quit_pending)
        self.assertEqual(app.holds, 1)
        # Failure does not schedule retries or override the external routes.
        self.assertEqual(self.scheduler.callbacks, {})
        app.runtime.audio.restore.side_effect = None
        app.request_quit()
        self.scheduler.run(self.scheduler.one(100))
        self.assertTrue(app.runtime.complete_work())
        self.assertEqual(app.runtime.audio.restore.call_count, 2)
        self.assertEqual(app.holds, 0)
        app.runtime.shutdown.assert_called_once()

    def test_no_journal_skips_restore_even_when_policy_is_enabled(self):
        app = ApplicationHarness(started=True, keep_aec=False)
        app.runtime.audio.journal.exists.return_value = False
        app.request_quit()
        self.scheduler.run(self.scheduler.one(100))
        app.runtime.audio.restore.assert_not_called()
        self.assertTrue(app.quitting)
        self.assertEqual(app.holds, 0)


if __name__ == '__main__':
    unittest.main()
