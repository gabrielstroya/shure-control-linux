# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Session reconciliation against FakePulse, without real devices or audio I/O."""
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from app.audio.pipewire import PipeWire
from app.core import ControlError, read_json, save_json
from test_core import FakePulse


SOURCE = 'alsa_input.test'
SINK = 'alsa_output.test'
OLD_SOURCE = 'shure_sin_eco'
OLD_SINK = 'altavoces_sin_eco'


def old_module():
    return {'index': 47, 'name': 'module-echo-cancel',
            'argument': (f'source_name={OLD_SOURCE} sink_name={OLD_SINK} '
                         f'source_master={SOURCE} sink_master={SINK} aec_method=webrtc')}


def stale_record():
    return {'version': 1, 'before': {'default_source': OLD_SOURCE,
                                    'default_sink': OLD_SINK,
                                    'modules': [old_module()]},
            'active': None, 'moves': [], 'expected_defaults': [OLD_SOURCE, OLD_SINK]}


class AECReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.fake = FakePulse()
        self.pulse = PipeWire(self.fake, self.directory / 'audio-session.json')

    def save(self, record=None):
        record = stale_record() if record is None else record
        save_json(self.pulse.journal, record)
        return record

    def state(self, result):
        self.assertIsInstance(result, dict)
        return result['state']

    def writes(self):
        return [args for args in self.fake.calls if len(args) > 1 and
                (args[1].startswith(('set-', 'move-')) or args[1] in ('load-module', 'unload-module'))]

    def archives(self):
        return [path for path in self.directory.rglob('*')
                if path.is_file() and path != self.pulse.journal]

    def assert_conflict_unchanged(self, data=None):
        before = self.pulse.journal.read_bytes()
        self.fake.calls.clear()
        result = self.pulse.reconcile(data)
        self.assertEqual(self.state(result), 'conflict')
        self.assertEqual(self.pulse.journal.read_bytes(), before)
        self.assertEqual(self.writes(), [])

    def test_no_journal_is_read_only_none(self):
        result = self.pulse.reconcile()
        self.assertEqual(self.state(result), 'none')
        self.assertFalse(self.pulse.journal.exists())
        self.assertEqual(self.archives(), [])
        self.assertEqual(self.writes(), [])

    def test_real_stale_adopted_session_archives_without_audio_mutation(self):
        original = self.save()
        result = self.pulse.reconcile()
        self.assertEqual(self.state(result), 'archived')
        record = read_json(self.pulse.journal)
        self.assertEqual(record['before']['default_source'], SOURCE)
        self.assertEqual(record['before']['default_sink'], SINK)
        self.assertEqual(record['expected_defaults'], [SOURCE, SINK])
        self.assertEqual(record['before']['modules'], [])
        self.assertIsNone(record['active'])
        self.assertFalse(record.get('pending_argument'))
        self.assertEqual(self.writes(), [])
        archives = self.archives()
        self.assertEqual(len(archives), 1)
        self.assertEqual(json.loads(archives[0].read_text()), original)
        self.assertEqual(stat.S_IMODE(archives[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.pulse.journal.stat().st_mode), 0o600)

    def test_reconciliation_is_idempotent(self):
        self.save()
        self.assertEqual(self.state(self.pulse.reconcile()), 'archived')
        before = self.pulse.journal.read_bytes()
        archives = self.archives()
        self.assertEqual(self.state(self.pulse.reconcile()), 'current')
        self.assertEqual(self.pulse.journal.read_bytes(), before)
        self.assertEqual(self.archives(), archives)
        self.assertEqual(self.writes(), [])

    def test_fresh_enable_disable_restore_never_recreates_old_adopted_aec(self):
        self.save()
        self.assertEqual(self.state(self.pulse.reconcile()), 'archived')
        self.pulse.enable(SOURCE, SINK)
        self.assertEqual(len(self.fake.modules), 1)
        self.pulse.disable()
        self.pulse.restore()
        self.assertEqual(self.fake.modules, [])
        self.assertEqual((self.fake.default_source, self.fake.default_sink), (SOURCE, SINK))
        loads = [args for args in self.fake.calls if args[1] == 'load-module']
        self.assertEqual(len(loads), 1)
        self.assertNotIn(f'source_name={OLD_SOURCE}', loads[0])
        self.assertFalse(self.pulse.journal.exists())
        self.assertEqual(len(self.archives()), 1)

    def test_lost_active_module_is_stale_even_when_defaults_match(self):
        record = stale_record()
        record.update(active=old_module(), expected_defaults=[SOURCE, SINK])
        self.save(record)
        self.assertEqual(self.state(self.pulse.reconcile()), 'archived')
        self.assertIsNone(read_json(self.pulse.journal)['active'])
        self.assertEqual(self.writes(), [])

    def test_lost_pending_load_is_stale_even_when_defaults_match(self):
        record = stale_record()
        record.update(pending_argument=old_module()['argument'], expected_defaults=[SOURCE, SINK])
        self.save(record)
        self.assertEqual(self.state(self.pulse.reconcile()), 'archived')
        self.assertFalse(read_json(self.pulse.journal).get('pending_argument'))
        self.assertEqual(self.writes(), [])

    def test_intentional_disable_preserves_original_adoption_rollback(self):
        self.fake(['pactl', 'load-module', 'module-echo-cancel',
                   f'source_name={OLD_SOURCE}', f'sink_name={OLD_SINK}',
                   f'source_master={SOURCE}', f'sink_master={SINK}', 'aec_method=webrtc'])
        self.fake.default_source, self.fake.default_sink = OLD_SOURCE, OLD_SINK
        self.pulse.enable(SOURCE, SINK, adopt=True)
        self.pulse.disable()
        before = self.pulse.journal.read_bytes()
        self.fake.calls.clear()
        self.assertEqual(self.state(self.pulse.reconcile()), 'current')
        self.assertEqual(self.pulse.journal.read_bytes(), before)
        self.assertEqual(self.archives(), [])
        self.assertEqual(self.writes(), [])
        self.pulse.restore()
        self.assertEqual(len(self.fake.modules), 1)
        self.assertEqual((self.fake.default_source, self.fake.default_sink), (OLD_SOURCE, OLD_SINK))

    def test_foreign_aec_prevents_reconciliation(self):
        self.save()
        self.fake(['pactl', 'load-module', 'module-echo-cancel',
                   'source_name=foreign_mic', 'sink_name=foreign_speakers',
                   f'source_master={SOURCE}', f'sink_master={SINK}', 'aec_method=webrtc'])
        self.assert_conflict_unchanged()
        self.assertEqual(len(self.fake.modules), 1)

    def test_owned_live_aec_with_external_defaults_is_not_rebased(self):
        self.pulse.enable(SOURCE, SINK)
        self.fake.default_source, self.fake.default_sink = SOURCE, SINK
        self.assert_conflict_unchanged()
        self.assertEqual(len(self.fake.modules), 1)

    def test_orphan_historical_nodes_prevent_reconciliation(self):
        for kind, name, index in [('sources', OLD_SOURCE, 30), ('sinks', OLD_SINK, 31)]:
            with self.subTest(kind=kind):
                self.save()
                getattr(self.fake, kind).append({'name': name, 'index': index})
                self.assert_conflict_unchanged()
                getattr(self.fake, kind).pop()

    def test_reserved_names_occupied_without_aec_are_not_assumed_owned(self):
        for kind, name, index in [('sources', 'scl_mic_aec', 30),
                                  ('sinks', 'scl_speakers_aec', 31)]:
            with self.subTest(kind=kind):
                self.save()
                getattr(self.fake, kind).append({'name': name, 'index': index})
                self.assert_conflict_unchanged()
                getattr(self.fake, kind).pop()

    def test_missing_or_nonphysical_defaults_are_conflicts(self):
        for name, exists in [('missing_input', False), ('external_virtual_input', True)]:
            with self.subTest(name=name):
                self.save()
                self.fake.default_source = name
                if exists:
                    self.fake.sources.append({'name': name, 'index': 30})
                self.assert_conflict_unchanged()
                if exists:
                    self.fake.sources.pop()
        self.fake.default_source = SOURCE

    def test_matching_but_missing_virtual_defaults_are_not_a_current_session(self):
        self.save()
        self.fake.default_source, self.fake.default_sink = OLD_SOURCE, OLD_SINK
        self.assert_conflict_unchanged()

    def test_live_defaults_changed_since_supplied_snapshot_are_not_adopted(self):
        self.save()
        data = self.pulse.snapshot()
        self.fake.sources.append({'name': 'alsa_input.other', 'index': 30})
        self.fake.default_source = 'alsa_input.other'
        self.assert_conflict_unchanged(data)

    def test_device_reusing_same_name_with_new_identity_is_a_conflict(self):
        self.save()
        data = self.pulse.snapshot()
        self.fake.sources[0]['index'] = 100
        self.assert_conflict_unchanged(data)

    def test_foreign_module_appearing_during_double_read_is_conflict(self):
        self.save()
        original_snapshot = self.pulse.snapshot
        counter = 0

        def snapshot():
            nonlocal counter
            counter += 1
            if counter == 2:
                self.fake.modules.append({'index': 99, 'name': 'module-echo-cancel',
                                          'argument': 'source_name=other sink_name=other_sink'})
            return original_snapshot()

        with patch.object(self.pulse, 'snapshot', side_effect=snapshot):
            self.assert_conflict_unchanged()
        self.assertEqual(len(self.fake.modules), 1)

    def test_concurrently_changed_journal_is_preserved(self):
        self.save()
        replacement = {**stale_record(), 'external_marker': 'do-not-overwrite'}
        original_snapshot = self.pulse.snapshot
        changed = False

        def snapshot():
            nonlocal changed
            if not changed:
                save_json(self.pulse.journal, replacement)
                changed = True
            return original_snapshot()

        with patch.object(self.pulse, 'snapshot', side_effect=snapshot):
            result = self.pulse.reconcile()
        self.assertEqual(self.state(result), 'conflict')
        self.assertEqual(read_json(self.pulse.journal), replacement)
        self.assertEqual(self.writes(), [])

    def test_rebased_volume_undo_is_retained_without_changing_volume(self):
        record = stale_record()
        record['volumes'] = {'source:' + SOURCE: {'kind': 'source', 'name': SOURCE,
                                                  'before': [65536], 'expected': [32768]}}
        self.fake.sources[0]['volume']['mono']['value'] = 32768
        self.save(record)
        self.assertEqual(self.state(self.pulse.reconcile()), 'archived')
        self.assertEqual(self.writes(), [])
        self.assertEqual(read_json(self.pulse.journal)['volumes'], record['volumes'])
        self.assertEqual(self.fake.sources[0]['volume']['mono']['value'], 32768)
        self.pulse.restore()
        self.assertEqual(self.fake.sources[0]['volume']['mono']['value'], 65536)
        self.assertEqual(self.fake.modules, [])

    def test_external_volume_change_guard_survives_reconciliation(self):
        record = stale_record()
        record['volumes'] = {'source:' + SOURCE: {'kind': 'source', 'name': SOURCE,
                                                  'before': [65536], 'expected': [32768]}}
        self.fake.sources[0]['volume']['mono']['value'] = 12345
        self.save(record)
        self.assertEqual(self.state(self.pulse.reconcile()), 'archived')
        self.assertEqual(self.writes(), [])
        with self.assertRaises(ControlError):
            self.pulse.restore()
        self.assertEqual(self.fake.sources[0]['volume']['mono']['value'], 12345)

    def test_only_live_moves_with_same_client_and_destination_are_retained(self):
        self.fake.sinks.append({'name': 'alsa_output.other', 'index': 3})
        self.fake.inputs = [{'index': 20, 'client': '42', 'sink': 2},
                            {'index': 21, 'client': 'different-client', 'sink': 2},
                            {'index': 22, 'client': '44', 'sink': 3}]
        base = {'kind': 'sink-inputs', 'field': 'sink', 'from': 'alsa_output.other', 'to': SINK}
        valid = {**base, 'index': 20, 'client': '42'}
        record = stale_record()
        record['moves'] = [valid, {**base, 'index': 21, 'client': '43'},
                           {**base, 'index': 22, 'client': '44'},
                           {**base, 'index': 23, 'client': '45'},
                           {**base, 'index': 20, 'client': '42', 'from': OLD_SINK}]
        self.save(record)
        self.assertEqual(self.state(self.pulse.reconcile()), 'archived')
        self.assertEqual(self.writes(), [])
        self.assertEqual(read_json(self.pulse.journal)['moves'], [valid])
        self.pulse.restore()
        self.assertEqual([stream['sink'] for stream in self.fake.inputs], [3, 2, 3])

    def test_failed_replacement_save_preserves_original_and_archive(self):
        original = self.save()
        before = self.pulse.journal.read_bytes()
        with patch('app.audio.pipewire.save_json', side_effect=OSError('Disk full')):
            with self.assertRaises(OSError):
                self.pulse.reconcile()
        self.assertEqual(self.pulse.journal.read_bytes(), before)
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(json.loads(self.archives()[0].read_text()), original)
        self.assertEqual(self.writes(), [])

    def test_journal_removed_during_archive_returns_conflict_without_recreating_it(self):
        original = self.save()

        def journal_removed(_fd):
            self.pulse.journal.unlink()

        with patch('app.audio.pipewire.os.fsync', side_effect=journal_removed):
            result = self.pulse.reconcile()
        self.assertEqual(self.state(result), 'conflict')
        self.assertFalse(self.pulse.journal.exists())
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(json.loads(self.archives()[0].read_text()), original)
        self.assertEqual(self.writes(), [])

    def test_restore_and_disable_refuse_missing_virtual_defaults_without_writes(self):
        for action in ('restore', 'disable'):
            with self.subTest(action=action):
                self.save()
                self.fake.default_source, self.fake.default_sink = OLD_SOURCE, OLD_SINK
                before = self.pulse.journal.read_bytes()
                self.fake.calls.clear()
                with self.assertRaises(ControlError):
                    getattr(self.pulse, action)()
                self.assertEqual(self.pulse.journal.read_bytes(), before)
                self.assertEqual(self.writes(), [])
                self.assertEqual(self.fake.modules, [])
        self.assertEqual(self.archives(), [])

    def test_restore_and_disable_refuse_orphan_nodes_without_writes(self):
        self.fake.sources.append({'name': OLD_SOURCE, 'index': 30})
        for action in ('restore', 'disable'):
            with self.subTest(action=action):
                self.save()
                before = self.pulse.journal.read_bytes()
                self.fake.calls.clear()
                with self.assertRaises(ControlError):
                    getattr(self.pulse, action)()
                self.assertEqual(self.pulse.journal.read_bytes(), before)
                self.assertEqual(self.writes(), [])
                self.assertIn(OLD_SOURCE, [device['name'] for device in self.fake.sources])
        self.assertEqual(self.archives(), [])

    def test_restore_and_disable_preserve_external_defaults_with_live_aec(self):
        self.pulse.enable(SOURCE, SINK)
        self.fake.sources.append({'name': 'alsa_input.other', 'index': 30})
        self.fake.default_source = 'alsa_input.other'
        before = self.pulse.journal.read_bytes()
        for action in ('restore', 'disable'):
            with self.subTest(action=action):
                self.fake.calls.clear()
                with self.assertRaises(ControlError):
                    getattr(self.pulse, action)()
                self.assertEqual(self.fake.default_source, 'alsa_input.other')
                self.assertEqual(len(self.fake.modules), 1)
                self.assertEqual(self.pulse.journal.read_bytes(), before)
                self.assertEqual(self.writes(), [])

    def test_audio_defaults_changed_during_archive_prevent_journal_replacement(self):
        original = self.save()
        before = self.pulse.journal.read_bytes()
        self.fake.sources.append({'name': 'alsa_input.other', 'index': 30})

        def defaults_changed(_fd):
            self.fake.default_source = 'alsa_input.other'

        with patch('app.audio.pipewire.os.fsync', side_effect=defaults_changed):
            result = self.pulse.reconcile()
        self.assertEqual(self.state(result), 'conflict')
        self.assertEqual(self.pulse.journal.read_bytes(), before)
        self.assertEqual(self.fake.default_source, 'alsa_input.other')
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(json.loads(self.archives()[0].read_text()), original)
        self.assertEqual(self.writes(), [])

    def test_aec_appearing_during_archive_prevents_journal_replacement(self):
        self.save()
        before = self.pulse.journal.read_bytes()

        def foreign_module_appears(_fd):
            self.fake.modules.append({'index': 99, 'name': 'module-echo-cancel',
                                      'argument': 'source_name=other sink_name=other_sink'})

        with patch('app.audio.pipewire.os.fsync', side_effect=foreign_module_appears):
            result = self.pulse.reconcile()
        self.assertEqual(self.state(result), 'conflict')
        self.assertEqual(self.pulse.journal.read_bytes(), before)
        self.assertEqual(len(self.fake.modules), 1)
        self.assertEqual(self.fake.modules[0]['index'], 99)
        self.assertEqual(self.writes(), [])

    def test_external_defaults_changed_during_load_are_preserved(self):
        self.fake.sources.append({'name': 'alsa_input.other', 'index': 30})
        original_load = self.pulse._load

        def load(argument):
            module = original_load(argument)
            self.fake.default_source = 'alsa_input.other'
            return module

        with patch.object(self.pulse, '_load', side_effect=load):
            with self.assertRaises(ControlError):
                self.pulse.enable(SOURCE, SINK)
        self.assertEqual(self.fake.default_source, 'alsa_input.other')
        self.assertEqual(self.fake.modules, [])
        self.assertFalse(any(args[1].startswith('set-default-') for args in self.fake.calls))

    def test_new_foreign_module_during_load_is_never_removed_by_cleanup(self):
        original_load = self.pulse._load

        def load(argument):
            module = original_load(argument)
            self.fake(['pactl', 'load-module', 'module-echo-cancel',
                       'source_name=foreign_mic', 'sink_name=foreign_speaker',
                       f'source_master={SOURCE}', f'sink_master={SINK}', 'aec_method=webrtc'])
            return module

        with patch.object(self.pulse, '_load', side_effect=load):
            with self.assertRaises(ControlError):
                self.pulse.enable(SOURCE, SINK)
        self.assertEqual(len(self.fake.modules), 1)
        self.assertIn('source_name=foreign_mic', self.fake.modules[0]['argument'])
        self.assertEqual((self.fake.default_source, self.fake.default_sink), (SOURCE, SINK))
        self.assertFalse(any(args[1].startswith('set-default-') for args in self.fake.calls))

    def test_reused_module_index_during_load_is_not_unloaded(self):
        original_load = self.pulse._load

        def load(argument):
            module = original_load(argument)
            self.fake.modules[0]['argument'] = (
                f'source_name=foreign_mic sink_name=foreign_speaker '
                f'source_master={SOURCE} sink_master={SINK} aec_method=webrtc')
            return module

        with patch.object(self.pulse, '_load', side_effect=load):
            with self.assertRaises(ControlError):
                self.pulse.enable(SOURCE, SINK)
        self.assertEqual(len(self.fake.modules), 1)
        self.assertIn('source_name=foreign_mic', self.fake.modules[0]['argument'])
        self.assertFalse(any(args[1] == 'unload-module' for args in self.fake.calls))

    def test_existing_foreign_use_of_new_virtual_node_prevents_cleanup(self):
        original_load = self.pulse._load

        def load(argument):
            module = original_load(argument)
            self.fake.default_source = 'scl_mic_aec'
            return module

        with patch.object(self.pulse, '_load', side_effect=load):
            with self.assertRaises(ControlError):
                self.pulse.enable(SOURCE, SINK)
        self.assertEqual(self.fake.default_source, 'scl_mic_aec')
        self.assertEqual(len(self.fake.modules), 1)
        self.assertFalse(any(args[1] == 'unload-module' for args in self.fake.calls))

    def replace_managed_module_with_foreign_at_same_index(self):
        self.fake.modules[0]['argument'] = (
            f'source_name=foreign_mic sink_name=foreign_speaker '
            f'source_master={SOURCE} sink_master={SINK} aec_method=webrtc')
        self.fake.sources = [device for device in self.fake.sources if device['name'] != 'scl_mic_aec']
        self.fake.sinks = [device for device in self.fake.sinks if device['name'] != 'scl_speakers_aec']
        if not any(device['name'] == 'foreign_mic' for device in self.fake.sources):
            self.fake.sources.append({'name': 'foreign_mic', 'index': 500})
            self.fake.sinks.append({'name': 'foreign_speaker', 'index': 501})

    def assert_foreign_module_and_journal_retained(self, module_index):
        self.assertEqual(len(self.fake.modules), 1)
        self.assertEqual(self.fake.modules[0]['index'], module_index)
        self.assertIn('source_name=foreign_mic', self.fake.modules[0]['argument'])
        self.assertFalse(any(args[1] == 'unload-module' for args in self.fake.calls))
        self.assertTrue(self.pulse.journal.is_file())
        saved = read_json(self.pulse.journal)
        self.assertIn('source_name=scl_mic_aec', saved['active']['argument'])

    def test_disable_does_not_unload_index_reused_during_stream_evacuation(self):
        self.pulse.enable(SOURCE, SINK)
        module_index = self.fake.modules[0]['index']
        self.fake.calls.clear()
        original = self.pulse._evacuate_streams

        def evacuate(*args):
            original(*args)
            self.replace_managed_module_with_foreign_at_same_index()

        with patch.object(self.pulse, '_evacuate_streams', side_effect=evacuate):
            with self.assertRaises(ControlError):
                self.pulse.disable()
        self.assert_foreign_module_and_journal_retained(module_index)

    def test_restore_does_not_unload_index_reused_during_undo_moves(self):
        self.pulse.enable(SOURCE, SINK)
        module_index = self.fake.modules[0]['index']
        self.fake.calls.clear()
        original = self.pulse._undo_moves

        def undo_moves(*args):
            original(*args)
            self.replace_managed_module_with_foreign_at_same_index()

        with patch.object(self.pulse, '_undo_moves', side_effect=undo_moves):
            with self.assertRaises(ControlError):
                self.pulse.restore()
        self.assert_foreign_module_and_journal_retained(module_index)


if __name__ == '__main__':
    unittest.main()
