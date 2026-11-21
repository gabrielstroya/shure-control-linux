# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from app.core import ControlError
from app.desktop.settings import APP_ID, Autostart, DEFAULTS, DesktopSettings


class DesktopSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = DesktopSettings(self.root / 'desktop.json')

    def test_default_load_does_not_write_or_enable_features(self):
        self.assertEqual(self.settings.load(), DEFAULTS)
        self.assertFalse(self.settings.path.exists())
        self.assertFalse(self.settings.load()['autostart'])
        self.assertFalse(self.settings.load()['resident_enabled'])

    def test_changes_survive_new_instance_and_preserve_unrelated_data(self):
        self.settings.path.write_text(json.dumps({'source': 'mic', 'sink': 'speaker', 'future': {'key': 1}}))
        self.settings.update(resident_enabled=True, close_behavior='hide')
        loaded = DesktopSettings(self.settings.path).load()
        self.assertTrue(loaded['resident_enabled'])
        self.assertEqual(loaded['close_behavior'], 'hide')
        self.assertEqual(loaded['source'], 'mic')
        self.assertEqual(loaded['sink'], 'speaker')
        self.assertEqual(loaded['future'], {'key': 1})
        self.assertEqual(self.settings.path.stat().st_mode & 0o777, 0o600)

    def test_existing_audio_preferences_are_untouched(self):
        audio = self.root / 'preferences.json'
        original = b'{"source":"mic", "sink":"speaker"}\n'
        audio.write_bytes(original)
        self.settings.update(keep_aec_on_exit=False)
        self.assertEqual(audio.read_bytes(), original)

    def test_invalid_updates_do_not_change_saved_bytes(self):
        self.settings.update(close_behavior='resident')
        original = self.settings.path.read_bytes()
        for patch in ({'resident_enabled': 1}, {'close_behavior': 'minimize'},
                      {'autostart': 'yes'}, {'shortcuts_enabled': None}, {'source': 'other'}):
            with self.subTest(patch=patch), self.assertRaises(ControlError):
                self.settings.update(**patch)
            self.assertEqual(self.settings.path.read_bytes(), original)

    def test_malformed_saved_data_is_preserved(self):
        for text in ('[]', '{broken', '{"autostart":1}'):
            self.settings.path.write_text(text)
            with self.assertRaises(ControlError):
                self.settings.update(autostart=True)
            self.assertEqual(self.settings.path.read_text(), text)


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.launcher = self.root / 'has space' / 'shure-control-linux'
        self.launcher.parent.mkdir()
        self.launcher.write_text('#!/usr/bin/python3\n# ' + APP_ID + '\n')
        self.launcher.chmod(0o755)
        self.autostart = Autostart(self.root / 'autostart' / (APP_ID + '.desktop'), self.launcher)

    def test_background_entry_enables_and_disables_only_owned_file(self):
        other = self.root / 'autostart' / 'unrelated.desktop'
        other.parent.mkdir()
        other.write_text('unrelated')
        self.assertFalse(self.autostart.is_enabled())
        self.assertTrue(self.autostart.set_enabled(True))
        self.assertTrue(self.autostart.is_enabled())
        content = self.autostart.path.read_text()
        self.assertIn(f'Exec="{self.launcher}" --background\n', content)
        self.assertIn(self.autostart.marker, content)
        self.assertFalse(self.autostart.set_enabled(False))
        self.assertFalse(self.autostart.path.exists())
        self.assertEqual(other.read_text(), 'unrelated')

    def test_refuses_missing_or_foreign_launcher(self):
        self.launcher.unlink()
        with self.assertRaises(ControlError):
            self.autostart.set_enabled(True)
        self.launcher.write_text('#!/bin/sh\nexit 0\n')
        self.launcher.chmod(0o755)
        with self.assertRaises(ControlError):
            self.autostart.set_enabled(True)
        self.assertFalse(self.autostart.path.exists())

    def test_unmarked_entry_is_never_replaced_or_deleted(self):
        self.autostart.path.parent.mkdir()
        self.autostart.path.write_text('[Desktop Entry]\nExec=unrelated\n')
        for enabled in (True, False):
            with self.assertRaises(ControlError):
                self.autostart.set_enabled(enabled)
        self.assertEqual(self.autostart.path.read_text(), '[Desktop Entry]\nExec=unrelated\n')

    def test_symlink_is_preserved(self):
        foreign = self.root / 'foreign'
        foreign.write_text('precious')
        self.autostart.path.parent.mkdir()
        self.autostart.path.symlink_to(foreign)
        for enabled in (True, False):
            with self.assertRaises(ControlError):
                self.autostart.set_enabled(enabled)
        self.assertTrue(self.autostart.path.is_symlink())
        self.assertEqual(foreign.read_text(), 'precious')

    def test_invalid_type_and_newline_path_are_rejected(self):
        with self.assertRaises(ControlError):
            self.autostart.set_enabled('false')
        with self.assertRaises(ControlError):
            Autostart._quoted_exec('/tmp/a\ninjected')

    def test_exec_with_special_characters_parses_as_single_path(self):
        try:
            from gi.repository import GLib
        except ImportError:
            self.skipTest('PyGObject no disponible en este intérprete')
        target = self.root / 'spaces % "quote" \\ slash $dollar `tick`'
        quoted = Autostart._quoted_exec(target)
        keyfile = GLib.KeyFile()
        content = '[Desktop Entry]\nType=Application\nName=Test\nExec=' + quoted + ' --background\n'
        keyfile.load_from_data(content, len(content), GLib.KeyFileFlags.NONE)
        ok, arguments = GLib.shell_parse_argv(keyfile.get_string('Desktop Entry', 'Exec'))
        self.assertTrue(ok)
        # Desktop field-code expansion subsequently turns %% into literal %.
        self.assertEqual(arguments, [str(target).replace('%', '%%'), '--background'])


class PortalShortcutTests(unittest.TestCase):
    def setUp(self):
        try:
            from app.desktop.shortcuts import PortalShortcuts
            self.actions, self.statuses = [], []
            self.portal = PortalShortcuts(self.actions.append, self.statuses.append)
        except ImportError:
            self.skipTest('PyGObject no disponible en este intérprete')

    def test_support_detection_does_not_bind_or_create_session(self):
        self.portal._call = Mock()
        self.portal._support(('<node><interface name="org.freedesktop.portal.GlobalShortcuts"/></node>',))
        self.assertTrue(self.portal.available)
        self.assertEqual(self.portal._call.call_args.args[1], 'Get')
        self.portal._call.reset_mock()
        self.portal._version((1,))
        self.portal._call.assert_not_called()
        self.assertIsNone(self.portal.session)
        self.assertFalse(self.portal.active)

    def test_missing_portal_explains_desktop_fallback(self):
        self.portal._support(('<node/>',))
        self.assertFalse(self.portal.available)
        self.assertIn('Configuración de Ubuntu', self.statuses[-1])

    def test_only_authorized_session_and_known_actions_dispatch(self):
        self.portal.active = True
        self.portal.session = '/session/ours'
        for session, action in (('/session/other', 'mute'), ('/session/ours', 'unknown'), ('/session/ours', 'open')):
            params = self.portal.GLib.Variant('(osta{sv})', (session, action, 123, {}))
            self.portal._activated(None, None, None, None, None, params, None)
        self.assertEqual(self.actions, ['open'])
        self.portal.active = False
        params = self.portal.GLib.Variant('(osta{sv})', ('/session/ours', 'mute', 124, {}))
        self.portal._activated(None, None, None, None, None, params, None)
        self.assertEqual(self.actions, ['open'])

    def test_bind_request_has_no_preselected_keyboard_combinations(self):
        self.portal._request = Mock()
        self.portal._created(0, {'session_handle': '/session/ours'})
        call = self.portal._request.call_args.args
        self.assertEqual(call[0], 'BindShortcuts')
        shortcuts = call[2][1]
        self.assertEqual({entry[0] for entry in shortcuts}, {'mute', 'open', 'profile-next'})
        self.assertTrue(all(set(entry[1]) == {'description'} for entry in shortcuts))

    def test_empty_binding_is_not_reported_active(self):
        self.portal._bound(0, {'shortcuts': []})
        self.assertFalse(self.portal.active)
        self.assertIn('no asignó', self.statuses[-1])

    def test_stop_invalidates_pending_callbacks_and_session(self):
        self.portal.connection = Mock()
        self.portal.connection.is_closed.return_value = False
        self.portal.session = '/session/ours'
        self.portal.active = True
        self.portal._subscriptions = [7, 8]
        connection = self.portal.connection
        generation = self.portal._generation
        self.portal.stop()
        self.assertEqual(self.portal._generation, generation + 1)
        self.assertIsNone(self.portal.session)
        self.assertFalse(self.portal.active)
        self.assertIsNone(self.portal.connection)
        self.assertEqual(connection.call.call_args.args[3], 'Close')
        self.assertEqual(connection.signal_unsubscribe.call_count, 2)


if __name__ == '__main__':
    unittest.main()
