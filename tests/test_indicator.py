# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
import copy
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from app.desktop.indicator import (ICON_DIR, ITEM_XML, MENU_XML, Indicator,
                                   MenuModel, device_state, icon_path)


SNAPSHOT = {
    'hardware': {'model': 'MV7', 'muted': False,
                 'settings': {'mode': 'manual', 'gain_db': 24.0}},
    'hardware_error': None, 'usb_present': True, 'busy': False,
    'aec_state': 'off', 'aec_managed': False,
    'profiles': [{'id': 'meeting', 'name': 'Videollamada'},
                 {'id': 'hugo', 'name': 'Hugo / AmparoLex'}],
    'meter_text': 'Nivel: sin captura activa', 'resident': True,
}


class MenuModelTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = copy.deepcopy(SNAPSHOT)
        self.model = MenuModel(self.snapshot)

    def test_actual_hardware_mute_changes_label_and_icon(self):
        self.assertEqual(self.model.state, 'connected')
        self.assertEqual(self.model.action_for(3), ('mute', None))
        self.snapshot['hardware']['muted'] = True
        self.model.update(self.snapshot)
        self.assertEqual(self.model.state, 'muted')
        self.assertEqual(self.model.items[3].properties['label'], 'Activar micrófono')
        self.assertIn('SILENCIADO', self.model.header)

    def test_disconnection_disables_hardware_commands(self):
        self.snapshot['usb_present'] = False
        self.model.update(self.snapshot)
        self.assertEqual(self.model.state, 'disconnected')
        for item in (3, 11, 12, 20, 41, 1000):
            self.assertIsNone(self.model.action_for(item))
        self.assertEqual(self.model.action_for(51), ('open', None))
        self.assertEqual(self.model.action_for(53), ('quit', None))

    def test_read_failure_never_displays_stale_muted_or_gain(self):
        self.snapshot['hardware']['muted'] = True
        self.snapshot['hardware_error'] = 'HID read failed'
        self.model.update(self.snapshot)
        self.assertEqual(self.model.state, 'error')
        self.assertNotIn('24', self.model.items[10].properties['label'])
        self.assertIsNone(self.model.action_for(3))

    def test_unknown_mute_does_not_claim_connected(self):
        del self.snapshot['hardware']['muted']
        self.assertEqual(device_state(self.snapshot), 'error')

    def test_original_model_identity_required(self):
        self.snapshot['hardware']['model'] = 'MV7+'
        self.assertEqual(device_state(self.snapshot), 'error')

    def test_gain_steps_and_bounds(self):
        self.assertEqual(self.model.action_for(11), ('gain', -1.5))
        self.assertEqual(self.model.action_for(12), ('gain', 1.5))
        self.snapshot['hardware']['settings']['gain_db'] = 0
        self.model.update(self.snapshot)
        self.assertIsNone(self.model.action_for(11))
        self.assertIsNotNone(self.model.action_for(12))
        self.snapshot['hardware']['settings']['gain_db'] = 36
        self.model.update(self.snapshot)
        self.assertIsNotNone(self.model.action_for(11))
        self.assertIsNone(self.model.action_for(12))

    def test_nonfinite_gain_disabled(self):
        self.snapshot['hardware']['settings']['gain_db'] = float('nan')
        self.model.update(self.snapshot)
        self.assertIsNone(self.model.action_for(11))
        self.assertIsNone(self.model.action_for(12))

    def test_auto_mode_never_changes_gain(self):
        self.snapshot['hardware']['settings']['mode'] = 'auto'
        self.model.update(self.snapshot)
        self.assertIn('automática', self.model.items[10].properties['label'])
        self.assertIsNone(self.model.action_for(11))
        self.assertIsNone(self.model.action_for(12))

    def test_busy_blocks_duplicate_mutations(self):
        self.snapshot['busy'] = True
        self.model.update(self.snapshot)
        for item in (3, 11, 12, 20, 41, 1000):
            self.assertIsNone(self.model.action_for(item))
        self.assertEqual(self.model.action_for(52), ('preferences', None))

    def test_foreign_aec_opens_review_without_adoption(self):
        for state in ('active', 'loaded', 'conflict', 'unknown'):
            with self.subTest(state=state):
                self.snapshot['aec_state'] = state
                self.model.update(self.snapshot)
                self.assertEqual(self.model.action_for(20), ('open', None))

    def test_managed_aec_can_be_stopped_after_usb_disconnect(self):
        self.snapshot.update(aec_state='active', aec_managed=True, usb_present=False)
        self.model.update(self.snapshot)
        self.assertEqual(self.model.action_for(20), ('aec', None))
        self.assertIn('Desactivar', self.model.items[20].properties['label'])

    def test_loaded_is_not_reported_as_active(self):
        self.snapshot.update(aec_state='loaded', aec_managed=True)
        self.model.update(self.snapshot)
        self.assertIn('sin ruta activa', self.model.items[20].properties['label'])

    def test_profile_ids_remain_stable_after_reordering(self):
        ids = {item.value: item.id for item in self.model.items.values()
               if item.action == 'profile'}
        self.snapshot['profiles'].reverse()
        self.model.update(self.snapshot)
        for profile, item_id in ids.items():
            self.assertEqual(self.model.action_for(item_id), ('profile', profile))
        self.snapshot['profiles'] = []
        self.model.update(self.snapshot)
        self.assertIsNone(self.model.action_for(ids['meeting']))

    def test_only_explicit_clicks_dispatch_and_measure_is_bounded(self):
        for event in ('opened', 'closed', 'hovered', 'invalid'):
            self.assertIsNone(self.model.action_for(3, event))
        self.assertIsNone(self.model.action_for(99999))
        self.assertIsNone(self.model.action_for(1))
        self.assertEqual(self.model.action_for(41), ('measure', None))
        self.assertIn('5 s', self.model.items[41].properties['label'])
        self.assertIn('sin captura activa', self.model.items[40].properties['label'])

    def test_original_icon_and_distinct_state_assets_exist(self):
        self.assertTrue((ICON_DIR / 'io.github.shurecontrol.Linux.svg').is_file())
        icons = []
        for state in ('connected', 'muted', 'disconnected', 'error'):
            text = Path(icon_path(state)).read_text()
            root = ET.fromstring(text)
            self.assertEqual(root.attrib['viewBox'], '0 0 128 128')
            icons.append(text)
        self.assertEqual(len(set(icons)), 4)


class DBusFormatTests(unittest.TestCase):
    """Validate real GLib serialization without a desktop host or audio I/O."""

    def setUp(self):
        try:
            from gi.repository import Gio, GLib
        except ImportError:
            self.skipTest('PyGObject no instalado en este intérprete')
        self.Gio, self.GLib = Gio, GLib
        self.actions = []
        self.indicator = Indicator(None, lambda *args: self.actions.append(args), lambda _: None)
        self.indicator.update(copy.deepcopy(SNAPSHOT))

    def test_introspection_and_layout_are_valid_gvariants(self):
        for xml in (ITEM_XML, MENU_XML):
            self.assertTrue(self.Gio.DBusNodeInfo.new_for_xml(xml).interfaces)
        layout = self.indicator._layout(0, -1, [])
        result = self.GLib.Variant('(u(ia{sv}av))', (1, layout))
        revision, root = result.unpack()
        self.assertEqual(revision, 1)
        self.assertEqual(root[0], 0)
        self.assertGreater(len(root[2]), 8)

    def test_depth_and_property_filters(self):
        root = self.indicator._layout(0, 0, ['label'])
        self.assertEqual(root, (0, {}, []))
        label = self.indicator._layout(10, 1, ['label'])
        self.assertEqual(set(label[1]), {'label'})
        self.assertEqual(len(label[2]), 2)

    def test_properties_are_typed(self):
        properties = self.indicator._item_properties()
        self.assertEqual(properties['Menu'].get_type_string(), 'o')
        self.assertEqual(properties['ToolTip'].get_type_string(), '(sa(iiay)ss)')
        self.assertEqual(properties['IconName'].unpack(), icon_path('connected'))

    def test_method_dispatch_uses_current_model_enablement(self):
        class Reply:
            def return_value(self, value):
                self.value = value

            def return_dbus_error(self, name, message):
                raise AssertionError((name, message))

        from app.desktop.indicator import MENU_IFACE, MENU_PATH
        args = self.GLib.Variant('(isvu)', (12, 'clicked', self.GLib.Variant('i', 0), 0))
        reply = Reply()
        self.indicator._method_call(None, None, MENU_PATH, MENU_IFACE, 'Event', args, reply)
        self.assertEqual(self.actions, [('gain', 1.5)])
        self.indicator.update({**SNAPSHOT, 'busy': True})
        self.indicator._method_call(None, None, MENU_PATH, MENU_IFACE, 'Event', args, reply)
        self.assertEqual(self.actions, [('gain', 1.5)])


if __name__ == '__main__':
    unittest.main()
