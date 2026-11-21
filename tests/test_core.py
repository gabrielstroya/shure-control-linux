# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
import array
import copy
import json
from pathlib import Path
import tempfile
import tomllib
import unittest

from app.audio.meter import Meter
from app.audio.pipewire import PipeWire
from app.controllers.shure import ShureController, validate
from app.core import ControlError, read_json
from app.profiles.store import Profiles

STATE = {'model': 'Shure MV7', 'serial': 'TEST', 'firmware_version': 'test', 'muted': False,
         'settings': {'mode': 'manual', 'gain_db': 24.0, 'monitor_mix': 8,
                      'eq_preset': 'flat', 'compressor': 'off', 'auto_position': 'near',
                      'auto_tone': 'natural', 'limiter_enabled': False,
                      'led_live_meter': False, 'led_night_mode': False}}


class FakeShure:
    def __init__(self):
        self.state = copy.deepcopy(STATE)
        self.calls = []
        self.ignore = False

    def __call__(self, args, env=None, timeout=None):
        self.calls.append(args)
        if '--mute' in args and not self.ignore:
            self.state['muted'] = args[args.index('--mute') + 1] == 'on'
        if '--preset' in args and not self.ignore:
            preset = tomllib.loads((Path(env['XDG_CONFIG_HOME']) / 'shurectl/presets/preset_1.toml').read_text())
            assert preset['monitor_mix_is_mic_level'] is False
            for key in self.state['settings']:
                if key in preset:
                    self.state['settings'][key] = preset[key]
        return json.dumps(self.state)


class HardwareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fake = FakeShure()
        self.controller = ShureController(runner=self.fake, state_path=Path(self.temp.name) / 'before.json')

    def test_model_identity_guard(self):
        self.fake.state['model'] = 'Shure MV7+'
        with self.assertRaises(ControlError):
            self.controller.read()

    def test_apply_preserves_unrelated_settings_and_mute(self):
        self.fake.state['muted'] = True
        result = self.controller.apply({'gain_db': 22.5})
        self.assertEqual(result['settings']['gain_db'], 22.5)
        self.assertEqual(result['settings']['monitor_mix'], 8)
        self.assertTrue(result['muted'])
        self.assertEqual(read_json(self.controller.state_path)['settings']['gain_db'], 24)

    def test_changed_settings_are_refused_before_write(self):
        old = self.controller.read()
        self.fake.state['settings']['gain_db'] = 12
        with self.assertRaises(ControlError):
            self.controller.apply({'gain_db': 22.5}, expected=old)
        self.assertFalse(any('--preset' in c for c in self.fake.calls))

    def test_auto_manual_boundaries(self):
        self.fake.state['settings']['mode'] = 'auto'
        with self.assertRaises(ControlError):
            self.controller.apply({'gain_db': 12})
        with self.assertRaises(ControlError):
            self.controller.apply({'mode': 'manual', 'auto_position': 'far'})

    def test_missing_optional_limiter_never_written(self):
        del self.fake.state['settings']['limiter_enabled']
        self.controller.read()
        with self.assertRaises(ControlError):
            self.controller.apply({'limiter_enabled': True})

    def test_mute_requires_readback(self):
        self.fake.ignore = True
        with self.assertRaises(ControlError):
            self.controller.mute(True)

    def test_gain_requires_readback(self):
        self.fake.ignore = True
        with self.assertRaises(ControlError):
            self.controller.apply({'gain_db': 12})

    def test_invalid_values(self):
        for patch in ({'gain_db': 37}, {'gain_db': 1}, {'gain_db': float('nan')},
                      {'monitor_mix': -1}, {'monitor_mix': 1.1}, {'firmware': 'write'},
                      {'limiter_enabled': 1}, {'eq_preset': 'bogus'}, {'gain_db': True}):
            with self.subTest(patch=patch), self.assertRaises(ControlError):
                validate(patch)

    def test_profiles_roundtrip_only_supported_fields(self):
        profiles = Profiles(Path(self.temp.name) / 'profiles.json')
        state = self.controller.read()
        profiles.save('hugo', state, 'alsa_input.test', 'alsa_output.test', True)
        profile = profiles.get('hugo')
        self.assertNotIn('muted', profile['settings'])
        self.assertNotIn('auto_position', profile['settings'])
        self.assertEqual(profile['settings']['gain_db'], 24)
        self.assertEqual(len(profiles.all()), 9)

    def test_restore_checks_same_device(self):
        state = self.controller.read()
        state['serial'] = 'DIFFERENT'
        with self.assertRaises(ControlError):
            self.controller.restore(state)

    def test_no_secret_in_profile(self):
        profiles = Profiles(Path(self.temp.name) / 'profiles.json')
        profiles.save('teams', self.controller.read(), 'alsa_input.test', 'alsa_output.test', True)
        self.assertNotIn('serial', profiles.path.read_text())


class FakePulse:
    def __init__(self):
        self.sources = [{'name': 'alsa_input.test', 'index': 1, 'volume': {'mono': {'value': 65536}}}]
        self.sinks = [{'name': 'alsa_output.test', 'index': 2}]
        self.modules = []
        self.default_source = 'alsa_input.test'
        self.default_sink = 'alsa_output.test'
        self.calls = []
        self.fail_sink = False
        self.counter = 50
        self.inputs = []
        self.outputs = []

    def __call__(self, args):
        self.calls.append(args)
        a = args[1:]
        if a[:2] == ['--format=json', 'list']:
            return json.dumps({'sources': self.sources, 'sinks': self.sinks, 'modules': self.modules,
                               'source-outputs': self.outputs, 'sink-inputs': self.inputs}[a[2]])
        if a[0] == 'get-default-source':
            return self.default_source
        if a[0] == 'get-default-sink':
            return self.default_sink
        if a[0] == 'set-default-source':
            if a[1] not in [s['name'] for s in self.sources]:
                raise ControlError('Missing source')
            self.default_source = a[1]
        elif a[0] == 'set-default-sink':
            if self.fail_sink or a[1] not in [s['name'] for s in self.sinks]:
                raise ControlError('Missing sink')
            self.default_sink = a[1]
        elif a[0] == 'set-source-volume':
            value = round(float(a[2][:-1])*65536/100) if a[2].endswith('%') else int(a[2])
            next(d for d in self.sources if d['name']==a[1])['volume']['mono']['value'] = value
        elif a[0] == 'load-module':
            self.counter += 1
            self.modules.append({'index': self.counter, 'name': a[1], 'argument': ' '.join(a[2:])})
            settings = dict(v.split('=', 1) for v in a[2:])
            self.sources.append({'index': self.counter * 2, 'name': settings['source_name']})
            self.sinks.append({'index': self.counter * 2 + 1, 'name': settings['sink_name']})
            return str(self.counter)
        elif a[0] == 'unload-module':
            module = next(m for m in self.modules if m['index'] == int(a[1]))
            settings = dict(v.split('=', 1) for v in module['argument'].split())
            self.sources = [s for s in self.sources if s['name'] != settings['source_name']]
            self.sinks = [s for s in self.sinks if s['name'] != settings['sink_name']]
            self.modules.remove(module)
        elif a[0].startswith('move-'):
            field = 'source' if a[0] == 'move-source-output' else 'sink'
            streams = self.outputs if field == 'source' else self.inputs
            devices = self.sources if field == 'source' else self.sinks
            stream = next(s for s in streams if str(s['index']) == a[1])
            stream[field] = next(d['index'] for d in devices if d['name'] == a[2])
        return ''


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fake = FakePulse()
        self.pulse = PipeWire(self.fake, Path(self.temp.name) / 'audio.json')

    def enable(self, adopt=False):
        return self.pulse.enable('alsa_input.test', 'alsa_output.test', adopt=adopt)

    def test_enable_idempotent_and_restore(self):
        self.enable()
        self.enable()
        self.assertEqual(len(self.fake.modules), 1)
        self.pulse.restore()
        self.assertFalse(self.fake.modules)
        self.assertEqual(self.fake.default_source, 'alsa_input.test')
        self.assertFalse(self.pulse.journal.exists())

    def test_disable_then_restore_external_aec(self):
        self.fake(['pactl', 'load-module', 'module-echo-cancel', 'source_name=external_mic',
                   'sink_name=external_speaker', 'source_master=alsa_input.test',
                   'sink_master=alsa_output.test', 'aec_method=webrtc'])
        self.fake.default_source, self.fake.default_sink = 'external_mic', 'external_speaker'
        self.enable(adopt=True)
        self.pulse.disable()
        self.assertFalse(self.fake.modules)
        self.assertEqual(self.fake.default_source, 'alsa_input.test')
        self.enable()
        self.pulse.disable()
        self.pulse.restore()
        self.assertEqual(len(self.fake.modules), 1)
        self.assertEqual(self.fake.default_source, 'external_mic')

    def test_unknown_aec_not_adopted_implicitly(self):
        self.enable()
        self.pulse.journal.unlink()
        with self.assertRaises(ControlError):
            self.enable()
        self.assertEqual(len(self.fake.modules), 1)

    def test_duplicates_refused(self):
        self.enable()
        self.fake.modules.append(copy.deepcopy(self.fake.modules[0]))
        with self.assertRaises(ControlError):
            self.enable(adopt=True)

    def test_external_defaults_preserved(self):
        self.enable()
        self.fake.default_source = 'alsa_input.other'
        with self.assertRaises(ControlError):
            self.pulse.disable()
        with self.assertRaises(ControlError):
            self.pulse.restore()
        self.assertEqual(self.fake.default_source, 'alsa_input.other')

    def test_failure_rolls_back_source_and_module(self):
        self.fake.fail_sink = True
        with self.assertRaises(ControlError):
            self.enable()
        self.assertFalse(self.fake.modules)
        self.assertEqual(self.fake.default_source, 'alsa_input.test')

    def test_pactl_json_without_module_indices(self):
        self.fake.modules = [{'name': 'libpipewire-module-native', 'argument': ''},
                             {'name': 'module-echo-cancel', 'argument': 'source_name=mic sink_name=speaker'}]
        base_runner = self.fake
        def runner(args):
            if args[1:] == ['list', 'short', 'modules']:
                return '1\tlibpipewire-module-native\t\n54321\tmodule-echo-cancel\tsource_name=mic sink_name=speaker\t\n'
            return base_runner(args)
        self.pulse.runner = runner
        modules = self.pulse.items('modules')
        self.assertEqual(self.pulse.echo_modules({'modules': modules})[0]['index'], 54321)
        self.assertNotIn('index', modules[0])

    def test_native_module_without_index_does_not_break_load(self):
        self.fake.modules = [{'name': 'libpipewire-module-native', 'argument': ''}]
        self.enable()
        self.assertEqual(len(self.pulse.echo_modules(self.pulse.snapshot())), 1)

    def test_existing_clients_survive_adopt_disable_restore(self):
        self.enable()
        virtual_index = self.fake.sinks[-1]['index']
        self.fake.inputs = [{'index': 27, 'client': '44', 'sink': virtual_index}]
        self.pulse.journal.unlink()
        self.enable(adopt=True)
        self.pulse.disable()
        self.assertEqual(self.fake.inputs[0]['sink'], 2)
        self.pulse.restore()
        self.assertEqual(self.fake.inputs[0]['sink'], self.fake.sinks[-1]['index'])

    def test_volume_restore_and_external_change_guard(self):
        self.pulse.set_volume('source', 'alsa_input.test', 80)
        self.assertEqual(self.fake.sources[0]['volume']['mono']['value'], round(65536*.8))
        self.pulse.restore()
        self.assertEqual(self.fake.sources[0]['volume']['mono']['value'], 65536)
        self.pulse.set_volume('source', 'alsa_input.test', 70)
        self.fake.sources[0]['volume']['mono']['value'] = 12345
        with self.assertRaises(ControlError):
            self.pulse.restore()
        self.assertEqual(self.fake.sources[0]['volume']['mono']['value'], 12345)

    def test_invalid_physical_device_refused(self):
        with self.assertRaises(ControlError):
            self.pulse.enable('anything; touch /tmp/no', 'alsa_output.test')
        self.assertFalse(self.fake.modules)

    def test_module_index_reuse_does_not_remove_other(self):
        self.enable()
        self.fake.modules[0]['argument'] = 'source_name=other'
        with self.assertRaises(ControlError):
            self.pulse.disable()
        self.assertEqual(len(self.fake.modules), 1)

    def test_restarted_module_found_by_fingerprint(self):
        self.enable()
        self.fake.modules[0]['index'] = 400
        self.pulse.disable()
        self.assertFalse(self.fake.modules)

    def test_streams_move_back_without_touching_changed_stream(self):
        self.fake.inputs = [{'index': 20, 'client': '42', 'sink': 2}, {'index': 21, 'client': '43', 'sink': 2}]
        self.enable()
        self.pulse.route_streams()
        self.fake.inputs[1]['sink'] = 999
        self.pulse.restore()
        self.assertEqual(self.fake.inputs[0]['sink'], 2)
        self.assertEqual(self.fake.inputs[1]['sink'], 999)


class MeterTests(unittest.TestCase):
    def test_real_math(self):
        peak, rms, clip = Meter.levels(array.array('f', [0.5, -0.5] * 50).tobytes())
        self.assertAlmostEqual(peak, -6.0206, places=3)
        self.assertAlmostEqual(rms, peak)
        self.assertFalse(clip)
        self.assertTrue(Meter.levels(array.array('f', [1.0]).tobytes())[2])
        self.assertEqual(Meter.levels(array.array('f', [0.0]).tobytes())[:2], (-90, -90))


if __name__ == '__main__':
    unittest.main()
