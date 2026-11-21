# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Resolved AEC warnings and USB state, using only fake local devices/audio."""
import copy
from pathlib import Path
import tempfile
import threading
import time
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from gi.repository import GLib

from app.controllers.runtime import ControllerRuntime, DEFAULTS_CONFLICT_NOTICE
from app.core import ControlError
from app.ui.window import Window
from app.ui.application import ShureApplication


WARNING = DEFAULTS_CONFLICT_NOTICE + ' No se sobrescribirán; revisa el diagnóstico.'
HARDWARE = {'model': 'MV7', 'muted': False, 'firmware_version': 'test',
            'settings': {'mode': 'manual', 'gain_db': 24.0}}
AUDIO = {'sources': [{'name': 'alsa_input.shure'}], 'sinks': [{'name': 'alsa_output.speaker'}],
         'modules': [], 'default_source': 'alsa_input.shure', 'default_sink': 'alsa_output.speaker'}


class FakeShure:
    def __init__(self):
        self.last_before = None
        self.read = Mock(side_effect=lambda: copy.deepcopy(HARDWARE))
        self.devices = Mock(return_value=[{'path': '/dev/fake', 'accessible': True}])
        self.apply = Mock()
        self.mute = Mock()
        self.restore = Mock()


class FakeAudio:
    def __init__(self, directory):
        self.journal = Path(directory) / 'audio-session.json'
        self.reconciliation = {'state': 'current'}
        self.snapshot_error = self.reconcile_error = None
        self.calls = []

    def snapshot(self):
        self.calls.append(('snapshot', threading.current_thread().name))
        if self.snapshot_error:
            raise ControlError(self.snapshot_error)
        return copy.deepcopy(AUDIO)

    def reconcile(self, data):
        self.calls.append(('reconcile', threading.current_thread().name, copy.deepcopy(data)))
        if self.reconcile_error:
            raise ControlError(self.reconcile_error)
        return dict(self.reconciliation)

    @staticmethod
    def echo_modules(data):
        return data['modules']

    @staticmethod
    def status(data):
        return 'Desactivado · sin módulos AEC' if not data['modules'] else 'Activo'


class AECStatusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.audio = FakeAudio(self.temporary.name)
        self.shure = FakeShure()
        self.meter = SimpleNamespace(stop=Mock(), visuals_enabled=False)
        profiles = SimpleNamespace(all=lambda: [])
        with patch('app.controllers.runtime.config_dir', return_value=Path(self.temporary.name)):
            self.runtime = ControllerRuntime(self.shure, self.audio, profiles, self.meter)
        self.messages = []
        self.runtime.message_listeners.append(self.messages.append)
        self.addCleanup(self.close_runtime)

    def drain(self):
        deadline = time.monotonic() + 2
        context = GLib.MainContext.default()
        while self.runtime.busy and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(.001)
        self.assertFalse(self.runtime.busy, 'No terminó el worker simulado')

    def close_runtime(self):
        self.drain()
        self.runtime.shutdown()

    def warning(self):
        self.runtime.operation_error = WARNING
        self.runtime.notify(WARNING)
        self.messages.clear()

    def refresh(self):
        self.runtime.refresh()
        self.drain()

    def test_archived_stale_session_clears_notice_after_fresh_inventory_in_worker(self):
        self.warning()
        self.audio.reconciliation = {'state': 'archived'}
        self.refresh()
        self.assertIsNone(self.runtime.operation_error)
        self.assertIsNone(self.runtime.last_message)
        self.assertEqual(self.messages, [None])
        self.assertEqual(self.runtime.hardware, HARDWARE)
        self.assertEqual(self.runtime.audio_data, AUDIO)
        self.assertEqual([call[0] for call in self.audio.calls], ['snapshot', 'reconcile'])
        self.assertTrue(self.audio.calls[1][1].startswith('shure-control'))
        self.assertEqual(self.audio.calls[1][2], AUDIO)
        self.shure.apply.assert_not_called()
        self.shure.mute.assert_not_called()
        self.shure.restore.assert_not_called()

    def test_current_defaults_or_absent_journal_resolve_only_old_defaults_notice(self):
        for state in ('current', 'none'):
            with self.subTest(state=state):
                self.warning()
                self.audio.reconciliation = {'state': state}
                self.refresh()
                self.assertIsNone(self.runtime.operation_error)
                self.assertEqual(self.messages, [None])

    def test_active_conflict_is_not_suppressed_by_refresh(self):
        self.warning()
        self.audio.reconciliation = {'state': 'conflict'}
        self.refresh()
        self.assertEqual(self.runtime.operation_error, WARNING)
        self.assertEqual(self.runtime.last_message, WARNING)
        self.assertEqual(self.messages, [])

    def test_failed_inventory_does_not_declare_old_conflict_resolved(self):
        self.warning()
        self.audio.snapshot_error = 'PipeWire no responde'
        self.refresh()
        self.assertEqual(self.runtime.operation_error, WARNING)
        self.assertEqual(self.runtime.audio_error, 'PipeWire no responde')
        self.assertEqual(self.runtime.hardware, HARDWARE)
        self.assertIsNone(self.runtime.hardware_error)
        self.assertEqual([call[0] for call in self.audio.calls], ['snapshot'])
        self.assertEqual(self.messages, [])

    def test_failed_journal_reconciliation_preserves_live_inventory_and_notice(self):
        self.warning()
        self.audio.reconcile_error = 'No se pudo conservar el respaldo'
        self.refresh()
        self.assertEqual(self.runtime.operation_error, WARNING)
        self.assertEqual(self.runtime.audio_data, AUDIO)
        self.assertEqual(self.runtime.audio_error, 'No se pudo conservar el respaldo')
        self.assertEqual(self.runtime.hardware, HARDWARE)
        self.assertIsNone(self.runtime.hardware_error)
        self.assertEqual(self.messages, [])

    def test_audio_operation_failure_never_marks_connected_mv7_disconnected(self):
        self.refresh()
        states = []
        self.runtime.listeners.append(lambda runtime: states.append((runtime.hardware, runtime.hardware_error)))
        self.audio.reconciliation = {'state': 'conflict'}

        def fail():
            raise ControlError(WARNING)

        self.runtime.work('Audio restaurado', fail)
        self.drain()
        self.assertEqual(self.runtime.operation_error, WARNING)
        self.assertTrue(states)
        self.assertTrue(all(hardware == HARDWARE and error is None for hardware, error in states))
        self.assertEqual(self.runtime.snapshot()['hardware'], HARDWARE)

    def test_success_and_consistent_refresh_clear_error_without_a_second_audio_operation(self):
        self.warning()
        operation = Mock(return_value=None)
        self.runtime.work('AEC actualizado', operation)
        self.drain()
        operation.assert_called_once()
        self.assertIsNone(self.runtime.operation_error)
        self.assertEqual(self.runtime.last_message, 'AEC actualizado')
        self.assertNotIn(WARNING, self.messages)

    def test_unrelated_success_does_not_erase_active_defaults_conflict(self):
        self.warning()
        self.audio.reconciliation = {'state': 'conflict'}
        self.runtime.work('Diagnóstico exportado', lambda: None)
        self.drain()
        self.assertEqual(self.runtime.operation_error, WARNING)
        view = SimpleNamespace(runtime=self.runtime, audio=self.audio)
        self.assertEqual(Window.current_status(view), WARNING)

    def test_other_warnings_are_not_cleared_by_defaults_reconciliation(self):
        warning = 'El volumen cambió fuera de la aplicación. No se sobrescribirá.'
        self.runtime.operation_error = warning
        self.runtime.notify(warning)
        self.messages.clear()
        self.audio.reconciliation = {'state': 'archived'}
        self.refresh()
        self.assertEqual(self.runtime.operation_error, warning)
        self.assertEqual(self.runtime.last_message, warning)
        self.assertEqual(self.messages, [])

    def test_archive_can_withdraw_notification_left_by_previous_process(self):
        self.audio.reconciliation = {'state': 'archived'}
        self.refresh()
        self.assertEqual(self.messages, [None])
        self.messages.clear()
        self.audio.reconciliation = {'state': 'current'}
        self.refresh()
        self.assertEqual(self.messages, [])

    def test_clearing_window_notice_dismisses_toast_and_shows_current_audio(self):
        self.refresh()
        previous = Mock()
        view = SimpleNamespace(runtime=self.runtime, audio=self.audio, active_toast=previous,
                               status=Mock(), toast=Mock())
        view.current_status = MethodType(Window.current_status, view)
        with patch('app.ui.window.Adw.Toast') as toast_factory:
            Window.show_message(view, None)
        previous.dismiss.assert_called_once()
        self.assertIsNone(view.active_toast)
        view.status.set_label.assert_called_once_with('MV7 conectado · Desactivado · sin módulos AEC')
        toast_factory.assert_not_called()
        view.toast.add_toast.assert_not_called()

    def test_visible_window_refresh_replaces_historical_footer_with_current_state(self):
        self.refresh()
        view = SimpleNamespace(runtime=self.runtime, audio=self.audio, closed=False, get_visible=lambda: True,
                               editing=False, screenshot_path=None, screenshot_done=False,
                               populate_hardware=Mock(), populate_audio=Mock(), update_enabled=Mock(),
                               status=Mock(), connection_row=Mock(), mute_row=Mock(), mute_button=Mock(), usb_row=Mock())
        view.current_status = MethodType(Window.current_status, view)
        view.status.get_label.return_value = WARNING
        Window.receive_state(view, self.runtime)
        view.status.set_label.assert_called_once_with('MV7 conectado · Desactivado · sin módulos AEC')

    def test_application_withdraws_old_desktop_notification_and_clears_hidden_window(self):
        window = SimpleNamespace(closed=False, show_message=Mock(), get_visible=lambda: False)
        application = SimpleNamespace(window=window, withdraw_notification=Mock(), send_notification=Mock())
        ShureApplication.on_message(application, None)
        application.withdraw_notification.assert_called_once_with('operation')
        window.show_message.assert_called_once_with(None)
        application.send_notification.assert_not_called()


if __name__ == '__main__':
    unittest.main()
