# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Controller lifecycle tests: fake devices/audio; no session bus or PCM I/O."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from gi.repository import GLib

from app.controllers.runtime import ControllerRuntime
from app.core import ControlError


HARDWARE = {'model': 'MV7', 'muted': False, 'serial': 'isolated-test',
            'settings': {'mode': 'manual', 'gain_db': 24.0}}


class FakeShure:
    def __init__(self):
        self.state = copy.deepcopy(HARDWARE)
        self.last_before = None
        self.calls = []
        self.block = None
        self.read_error = None

    def read(self):
        self.calls.append('read')
        if self.block:
            self.block.wait(1)
        if self.read_error:
            raise ControlError(self.read_error)
        return copy.deepcopy(self.state)

    def devices(self):
        return [{'path': '/dev/fake-mv7', 'accessible': True}]

    def mute(self, muted):
        self.calls.append(('mute', muted))
        self.last_before = self.read()
        self.state['muted'] = muted
        return self.read()

    def apply(self, values, expected=None):
        self.calls.append(('apply', dict(values)))
        if expected is not None and expected != self.state:
            raise ControlError('El estado cambió.')
        self.last_before = self.read()
        self.state['settings'].update(values)
        return self.read()

    def restore(self, before):
        self.calls.append('restore')
        self.state = copy.deepcopy(before)
        return self.read()


class FakeAudio:
    def __init__(self, directory):
        self.journal = Path(directory) / 'session.json'
        self.data = {'sources': [{'name': 'alsa_input.shure'}],
                     'sinks': [{'name': 'alsa_output.hdmi'}], 'modules': [],
                     'default_source': 'alsa_input.shure', 'default_sink': 'alsa_output.hdmi'}
        self.calls = []
        self.enable_error = None

    def snapshot(self):
        self.calls.append('snapshot')
        return copy.deepcopy(self.data)

    def physical(self, data, kind):
        return data[kind]

    def echo_modules(self, data):
        return data['modules']

    def validate_pair(self, source, sink, data=None):
        data = data or self.data
        if source not in [d['name'] for d in data['sources']] or sink not in [d['name'] for d in data['sinks']]:
            raise ControlError('Ruta no disponible')

    def enable(self, source, sink):
        self.calls.append(('enable', source, sink))
        if self.enable_error:
            raise ControlError(self.enable_error)

    def disable(self):
        self.calls.append('disable')
        raise ControlError('No se administra el módulo externo.')

    def _active(self, record, data):
        return None


class FakeProfiles:
    def __init__(self):
        self.profiles = [{'id': 'meeting', 'name': 'Videollamada', 'aec': True,
                          'settings': {'mode': 'manual', 'gain_db': 18.0},
                          'customized': True}]

    def all(self):
        return copy.deepcopy(self.profiles)

    def get(self, key):
        return next(p for p in self.all() if p['id'] == key)


class FakeMeter:
    def __init__(self):
        self.process = None
        self.visuals_enabled = False
        self.error = None
        self.peak, self.rms = -12.0, -24.0
        self.calls = []

    def start(self, source):
        self.calls.append(('start', source))
        self.process = object()

    def stop(self):
        self.calls.append('stop')
        self.process = None


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch('app.controllers.runtime.config_dir', return_value=Path(self.temp.name)):
            self.runtime = ControllerRuntime(FakeShure(), FakeAudio(self.temp.name),
                                             FakeProfiles(), FakeMeter())
        self.messages = []
        self.runtime.message_listeners.append(self.messages.append)
        self.addCleanup(self.cleanup_runtime)

    def drain(self):
        deadline = time.monotonic() + 2
        context = GLib.MainContext.default()
        while self.runtime.busy and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(.002)
        self.assertFalse(self.runtime.busy, 'El worker o su callback no terminaron')

    def cleanup_runtime(self):
        if self.runtime.shure.block:
            self.runtime.shure.block.set()
        self.drain()
        if not self.runtime.closed:
            self.runtime.shutdown()

    def test_poll_reads_external_changes_without_capture_or_writes(self):
        self.runtime.refresh()
        self.drain()
        self.assertEqual(self.runtime.hardware['settings']['gain_db'], 24)
        self.runtime.shure.state['muted'] = True
        self.runtime.shure.state['settings']['gain_db'] = 10.5
        self.runtime.refresh()
        self.drain()
        self.assertTrue(self.runtime.hardware['muted'])
        self.assertEqual(self.runtime.hardware['settings']['gain_db'], 10.5)
        self.assertEqual(self.runtime.meter.calls, [])
        self.assertTrue(all(call == 'read' for call in self.runtime.shure.calls))
        self.assertTrue(all(call == 'snapshot' for call in self.runtime.audio.calls))

    def test_mute_uses_fresh_hardware_value_and_readback(self):
        self.runtime.hardware = copy.deepcopy(HARDWARE)
        self.runtime.shure.state['muted'] = True
        self.assertTrue(self.runtime.toggle_mute())
        self.drain()
        self.assertIn(('mute', False), self.runtime.shure.calls)
        self.assertFalse(self.runtime.hardware['muted'])
        self.assertTrue(self.runtime.previous_hardware['muted'])

    def test_worker_serializes_poll_and_action_and_rejects_second_action(self):
        self.runtime.shure.block = threading.Event()
        self.runtime.refresh()
        self.assertTrue(self.runtime.toggle_mute())
        self.assertFalse(self.runtime.adjust_gain(1.5))
        self.assertTrue(self.runtime.action_busy)
        self.runtime.shure.block.set()
        self.drain()
        self.assertTrue(self.runtime.hardware['muted'])
        self.assertFalse(any(isinstance(call, tuple) and call[0] == 'apply'
                             for call in self.runtime.shure.calls))

    def test_manual_gain_clamps_to_device_limit(self):
        self.runtime.shure.state['settings']['gain_db'] = 36
        self.runtime.adjust_gain(1.5)
        self.drain()
        self.assertEqual(self.runtime.hardware['settings']['gain_db'], 36)

    def test_automatic_gain_refuses_manual_write(self):
        self.runtime.shure.state['settings']['mode'] = 'auto'
        self.runtime.adjust_gain(1.5)
        self.drain()
        self.assertTrue(any('modo manual' in message for message in self.messages))
        self.assertFalse(any(isinstance(call, tuple) and call[0] == 'apply'
                             for call in self.runtime.shure.calls))

    def test_read_failure_clears_stale_state(self):
        self.runtime.hardware = copy.deepcopy(HARDWARE)
        self.runtime.shure.read_error = 'Sin acceso HID'
        self.runtime.refresh()
        self.drain()
        snapshot = self.runtime.snapshot()
        self.assertIsNone(snapshot['hardware'])
        self.assertEqual(snapshot['hardware_error'], 'Sin acceso HID')
        self.assertTrue(snapshot['usb_present'])

    def test_profile_rolls_back_hardware_if_aec_fails(self):
        self.runtime.audio.enable_error = 'No se pudo activar AEC'
        self.runtime.apply_profile('meeting')
        self.drain()
        self.assertIn('restore', self.runtime.shure.calls)
        self.assertEqual(self.runtime.shure.state, HARDWARE)
        self.assertIsNone(self.runtime.last_profile)

    def test_foreign_aec_blocks_profile_before_hardware_write(self):
        self.runtime.audio.data['modules'] = [{'name': 'module-echo-cancel', 'argument': ''}]
        self.runtime.apply_profile('meeting')
        self.drain()
        self.assertFalse(any(isinstance(call, tuple) and call[0] == 'apply'
                             for call in self.runtime.shure.calls))
        self.assertNotIn('disable', self.runtime.audio.calls)

    def test_quick_measurement_is_explicit_bounded_and_has_no_visuals(self):
        self.runtime.audio_data = self.runtime.audio.snapshot()
        self.runtime.start_measurement()
        self.assertTrue(self.runtime.measure_timer)
        self.assertIsNotNone(self.runtime.meter.process)
        self.assertFalse(self.runtime.meter.visuals_enabled)
        # Simulate the timeout without waiting five seconds or opening audio.
        GLib.source_remove(self.runtime.measure_timer)
        self.runtime.finish_measurement()
        self.assertEqual(self.runtime.measure_timer, 0)
        self.assertIsNone(self.runtime.meter.process)
        self.assertIn('Peak -12.0 / RMS -24.0', self.runtime.meter_text)

    def test_measurement_requires_only_a_source_not_an_hdmi_sink(self):
        self.runtime.audio_data = self.runtime.audio.snapshot()
        self.runtime.audio_data['sinks'] = [{'name': 'alsa_output.analog-stereo'}]
        self.runtime.start_measurement()
        self.assertEqual(self.runtime.meter.calls, [('start', 'alsa_input.shure')])
        self.assertTrue(self.runtime.measure_timer)

    def test_cancelled_measurement_does_not_claim_capture_is_running(self):
        self.runtime.audio_data = self.runtime.audio.snapshot()
        self.runtime.start_measurement()
        self.runtime.stop_meter()
        self.assertIsNone(self.runtime.meter.process)
        self.assertEqual(self.runtime.measure_timer, 0)
        self.assertNotIn('Midiendo', self.runtime.meter_text)

    def test_capture_start_failure_has_no_timer_or_running_label(self):
        self.runtime.audio_data = self.runtime.audio.snapshot()

        def fail_start(_source):
            self.runtime.meter.error = 'No se pudo iniciar la captura'
            self.runtime.meter.process = None

        self.runtime.meter.start = fail_start
        self.runtime.start_measurement()
        self.assertIsNone(self.runtime.meter.process)
        self.assertEqual(self.runtime.measure_timer, 0)
        self.assertNotIn('Midiendo', self.runtime.meter_text)
        self.assertTrue(any('captura' in message for message in self.messages))

    def test_stop_cancels_measurement_timer_and_shutdown_is_final(self):
        self.runtime.audio_data = self.runtime.audio.snapshot()
        self.runtime.start_measurement()
        self.runtime.shutdown()
        self.assertTrue(self.runtime.closed)
        self.assertFalse(self.runtime.measure_timer)
        self.assertIsNone(self.runtime.meter.process)
        self.assertFalse(self.runtime.refresh())
        self.assertFalse(self.runtime.toggle_mute())


if __name__ == '__main__':
    unittest.main()
