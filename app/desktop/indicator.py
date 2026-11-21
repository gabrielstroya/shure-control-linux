# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""StatusNotifierItem and dbusmenu export without a GTK3/AppIndicator dependency.

Only this adapter knows D-Bus. MenuModel is independent of GTK, USB and PipeWire;
callbacks are routed to the application's existing, serialized controller.
All public methods and callbacks run on the application's GLib main context.
"""
from dataclasses import dataclass, field
import logging
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ICON_DIR = ROOT / 'packaging/icons'
ITEM_PATH = '/StatusNotifierItem'
MENU_PATH = '/StatusNotifierItem/Menu'
ITEM_IFACE = 'org.kde.StatusNotifierItem'
MENU_IFACE = 'com.canonical.dbusmenu'
WATCHER = 'org.kde.StatusNotifierWatcher'
WATCHER_PATH = '/StatusNotifierWatcher'

ITEM_XML = '''<node><interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionMovieName" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  <signal name="NewTitle"/><signal name="NewIcon"/><signal name="NewToolTip"/>
  <signal name="NewStatus"><arg type="s"/></signal>
</interface></node>'''

MENU_XML = '''<node><interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/>
    <arg type="as" direction="in"/><arg type="u" direction="out"/>
    <arg type="(ia{sv}av)" direction="out"/></method>
  <method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
    <arg type="a(ia{sv})" direction="out"/></method>
  <method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/>
    <arg type="v" direction="out"/></method>
  <method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/>
    <arg type="v" direction="in"/><arg type="u" direction="in"/></method>
  <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/>
    <arg type="ai" direction="out"/></method>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
  <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
</interface></node>'''


def device_state(snapshot):
    """Never show stale hardware values as confirmed after a read failure."""
    if not snapshot.get('usb_present'):
        return 'disconnected'
    state = snapshot.get('hardware')
    if (snapshot.get('hardware_error') or not isinstance(state, dict)
            or state.get('model') != 'MV7' or type(state.get('muted')) is not bool):
        return 'error'
    return 'muted' if state['muted'] else 'connected'


def icon_path(state):
    return str(ICON_DIR / f'io.github.shurecontrol.Linux-{state}.svg')


@dataclass
class MenuItem:
    id: int
    properties: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    action: str | None = None
    value: object = None


class MenuModel:
    """Stable menu IDs, live enablement, and no optimistic device state."""

    def __init__(self, snapshot=None):
        self._profile_ids = {}
        self.items = {}
        self.state = 'disconnected'
        self.header = ''
        self.description = ''
        self.update(snapshot or {})

    def update(self, snapshot):
        self.state = device_state(snapshot)
        connected = self.state in ('connected', 'muted')
        busy = bool(snapshot.get('busy'))
        writable = connected and not busy
        hardware = snapshot.get('hardware') or {}
        settings = hardware.get('settings') or {}
        manual = settings.get('mode') == 'manual'
        gain = settings.get('gain_db')
        valid_gain = (type(gain) in (int, float) and math.isfinite(gain)
                      and 0 <= gain <= 36)
        self.header = {
            'connected': 'SHURE MV7 — CONECTADO',
            'muted': 'SHURE MV7 — SILENCIADO',
            'disconnected': 'SHURE MV7 — DESCONECTADO',
            'error': 'SHURE MV7 — ESTADO NO DISPONIBLE',
        }[self.state]
        gain_label = ('Ganancia: ' + f'{gain:.1f}'.replace('.', ',') + ' dB'
                      if connected and manual and valid_gain else
                      'Ganancia: automática' if connected and not manual else
                      'Ganancia: no disponible')
        aec_state = snapshot.get('aec_state', 'unknown')
        aec_managed = bool(snapshot.get('aec_managed'))
        aec_action = 'aec'
        if aec_state == 'active' and aec_managed:
            aec_label = 'Desactivar AEC — activo'
        elif aec_state == 'loaded' and aec_managed:
            aec_label = 'Desactivar AEC — cargado, sin ruta activa'
        elif aec_state in ('active', 'loaded'):
            aec_label, aec_action = 'AEC externo — revisar…', 'open'
        elif aec_state == 'off':
            aec_label = 'Activar cancelación de eco — inactiva'
        elif aec_state == 'conflict':
            aec_label, aec_action = 'AEC en conflicto — revisar…', 'open'
        else:
            aec_label, aec_action = 'AEC no disponible — revisar…', 'open'

        def row(item_id, label='', enabled=True, action=None, value=None, children=None):
            props = {'label': label, 'enabled': bool(enabled), 'visible': True}
            if children is not None:
                props['children-display'] = 'submenu'
            return MenuItem(item_id, props, children or [], action, value)

        def separator(item_id):
            return MenuItem(item_id, {'type': 'separator', 'visible': True})

        gain_rows = [
            row(11, 'Bajar 1,5 dB', writable and manual and valid_gain and gain > 0,
                'gain', -1.5),
            row(12, 'Subir 1,5 dB', writable and manual and valid_gain and gain < 36,
                'gain', 1.5),
        ]
        profile_rows = []
        for profile in snapshot.get('profiles') or []:
            profile_id, name = profile.get('id'), profile.get('name')
            if not isinstance(profile_id, str) or not isinstance(name, str):
                continue
            if profile_id not in self._profile_ids:
                self._profile_ids[profile_id] = 1000 + len(self._profile_ids)
            profile_rows.append(row(self._profile_ids[profile_id], name, writable,
                                    'profile', profile_id))
        if not profile_rows:
            profile_rows.append(row(31, 'No hay perfiles disponibles', False))
        meter_text = snapshot.get('meter_text') or 'Nivel: sin captura activa'
        children = [
            row(1, self.header, False), separator(2),
            row(3, 'Activar micrófono' if self.state == 'muted' else 'Silenciar micrófono',
                writable, 'mute'),
            row(10, gain_label, connected, children=gain_rows),
            row(20, aec_label, aec_action == 'open' or
                (not busy and (connected or aec_managed)), aec_action),
            row(30, 'Perfiles de audio', connected, children=profile_rows),
            separator(39), row(40, str(meter_text), False),
            row(41, 'Medir entrada durante 5 s', writable, 'measure'),
            separator(50),
            row(51, 'Abrir Shure Control Linux', True, 'open'),
            row(52, 'Preferencias…', True, 'preferences'),
            row(53, 'Salir completamente', True, 'quit'),
        ]
        if busy:
            children.insert(2, row(4, 'Operación en curso…', False))
        self.root = MenuItem(0, {'children-display': 'submenu'}, children)
        self.items = {}

        def index(item):
            self.items[item.id] = item
            for child in item.children:
                index(child)

        index(self.root)
        self.description = '\n'.join((self.header, gain_label, aec_label, str(meter_text)))

    def action_for(self, item_id, event='clicked'):
        item = self.items.get(item_id)
        if (event != 'clicked' or item is None or not item.properties.get('enabled', True)
                or not item.properties.get('visible', True) or not item.action):
            return None
        return item.action, item.value


class Indicator:
    """Session-bus indicator. Availability means the host accepted registration.

    No audio I/O, subprocesses, polling or application lifetime ownership lives
    here. Unsupported desktops are reported through on_availability(False).
    """

    def __init__(self, application, on_action, on_availability):
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.application = application
        self.on_action, self.on_availability = on_action, on_availability
        self.model = MenuModel()
        self._connection = None
        self._exports = []
        self._watch_id = 0
        self._signal_id = 0
        self._owner = None
        self._registered = False
        self._host_present = False
        self._available = None
        self._revision = 1
        self._running = False
        self._epoch = 0

    def start(self):
        if self._running:
            return
        self._running = True
        try:
            # Own a connection so stop() also drops the registered unique name.
            # Unexporting objects on Gtk.Application's shared connection alone
            # can leave a stale icon in hosts until the whole process exits.
            address = self.Gio.dbus_address_get_for_bus_sync(self.Gio.BusType.SESSION, None)
            self._connection = self.Gio.DBusConnection.new_for_address_sync(
                address, self.Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
                self.Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
            self._connection.set_exit_on_close(False)
            for path, xml in ((ITEM_PATH, ITEM_XML), (MENU_PATH, MENU_XML)):
                info = self.Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
                self._exports.append(self._connection.register_object(
                    path, info, self._method_call, self._property, None))
            self._signal_id = self._connection.signal_subscribe(
                WATCHER, WATCHER, None, WATCHER_PATH, None,
                self.Gio.DBusSignalFlags.NONE, self._watcher_signal)
            self._watch_id = self.Gio.bus_watch_name_on_connection(
                self._connection, WATCHER, self.Gio.BusNameWatcherFlags.NONE,
                self._watcher_appeared, self._watcher_vanished)
        except Exception:
            logging.exception('No se pudo iniciar el indicador de escritorio')
            self.stop()

    def stop(self):
        self._running = False
        self._epoch += 1
        if self._watch_id:
            self.Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        if self._connection:
            if self._signal_id:
                self._connection.signal_unsubscribe(self._signal_id)
                self._signal_id = 0
            for export_id in self._exports:
                self._connection.unregister_object(export_id)
            if not self._connection.is_closed():
                self._connection.close(None, None, None)
        self._exports.clear()
        self._connection = None
        self._owner = None
        self._registered = self._host_present = False
        self._set_available(False)

    def _set_available(self, available):
        if self._available is not bool(available):
            self._available = bool(available)
            self.on_availability(self._available)

    def _watcher_appeared(self, connection, name, owner):
        self._epoch += 1
        epoch = self._epoch
        self._owner = owner
        self._registered = self._host_present = False
        self._set_available(False)

        def registered(connection, result):
            if not self._running or epoch != self._epoch:
                return
            try:
                connection.call_finish(result)
                self._registered = True
            except self.GLib.Error as exc:
                logging.warning('El anfitrión rechazó el indicador: %s', exc.message)
                return
            connection.call(owner, WATCHER_PATH, 'org.freedesktop.DBus.Properties',
                            'Get', self.GLib.Variant('(ss)',
                            (WATCHER, 'IsStatusNotifierHostRegistered')),
                            self.GLib.VariantType.new('(v)'), self.Gio.DBusCallFlags.NONE,
                            3000, None, host_checked)

        def host_checked(connection, result):
            if not self._running or epoch != self._epoch:
                return
            try:
                self._host_present = bool(connection.call_finish(result).unpack()[0])
            except self.GLib.Error:
                self._host_present = False
            self._set_available(self._registered and self._host_present)

        connection.call(owner, WATCHER_PATH, WATCHER, 'RegisterStatusNotifierItem',
                        self.GLib.Variant('(s)', (ITEM_PATH,)), None,
                        self.Gio.DBusCallFlags.NONE, 3000, None, registered)

    def _watcher_vanished(self, connection, name):
        self._epoch += 1
        self._owner = None
        self._registered = self._host_present = False
        self._set_available(False)

    def _watcher_signal(self, connection, sender, path, interface, name, parameters):
        if sender != self._owner:
            return
        if name == 'StatusNotifierHostRegistered':
            self._host_present = True
        elif name == 'StatusNotifierHostUnregistered':
            self._host_present = False
        else:
            return
        self._set_available(self._registered and self._host_present)

    def update(self, snapshot):
        previous_items = self.model.items
        previous_state, previous_description = self.model.state, self.model.description
        self.model.update(snapshot)
        if self.model.items != previous_items:
            self._revision = self._revision % 0xffffffff + 1
            self._emit(MENU_PATH, MENU_IFACE, 'LayoutUpdated', '(ui)', (self._revision, 0))
        changed = {}
        if previous_state != self.model.state:
            changed['IconName'] = self.GLib.Variant('s', icon_path(self.model.state))
            self._emit(ITEM_PATH, ITEM_IFACE, 'NewIcon')
        if previous_description != self.model.description:
            changed['ToolTip'] = self._item_properties()['ToolTip']
            self._emit(ITEM_PATH, ITEM_IFACE, 'NewToolTip')
        if changed:
            self._emit(ITEM_PATH, 'org.freedesktop.DBus.Properties', 'PropertiesChanged',
                       '(sa{sv}as)', (ITEM_IFACE, changed, []))

    def _emit(self, path, interface, name, signature=None, values=()):
        if self._connection and self._running:
            self._connection.emit_signal(None, path, interface, name,
                                         self.GLib.Variant(signature, values) if signature else None)

    def _item_properties(self):
        v = self.GLib.Variant
        icon = icon_path(self.model.state)
        return {
            'Category': v('s', 'Hardware'), 'Id': v('s', 'shure-control-linux'),
            'Title': v('s', 'Shure Control Linux'), 'Status': v('s', 'Active'),
            'WindowId': v('i', 0), 'IconThemePath': v('s', str(ICON_DIR)),
            'Menu': v('o', MENU_PATH), 'ItemIsMenu': v('b', True),
            'IconName': v('s', icon), 'IconPixmap': v('a(iiay)', []),
            'OverlayIconName': v('s', ''), 'OverlayIconPixmap': v('a(iiay)', []),
            'AttentionIconName': v('s', icon_path('error')),
            'AttentionIconPixmap': v('a(iiay)', []), 'AttentionMovieName': v('s', ''),
            'ToolTip': v('(sa(iiay)ss)', (icon, [], 'Shure Control Linux', self.model.description)),
        }

    def _property(self, connection, sender, path, interface, name):
        if interface == ITEM_IFACE:
            return self._item_properties().get(name)
        v = self.GLib.Variant
        return {'Version': v('u', 3), 'TextDirection': v('s', 'ltr'),
                'Status': v('s', 'normal'), 'IconThemePath': v('as', [str(ICON_DIR)])}.get(name)

    def _props(self, item, names=()):
        return {key: self.GLib.Variant('b' if type(value) is bool else 's', value)
                for key, value in item.properties.items() if not names or key in names}

    def _layout(self, item_id, depth, names):
        item = self.model.items[item_id]
        children = [] if depth == 0 else [self.GLib.Variant('(ia{sv}av)',
            self._layout(child.id, depth - 1, names)) for child in item.children]
        return item.id, self._props(item, names), children

    def _dispatch(self, item_id, event):
        action = self.model.action_for(item_id, event)
        if action:
            self.on_action(*action)

    def _method_call(self, connection, sender, path, interface, name, parameters, invocation):
        v = self.GLib.Variant
        try:
            args = parameters.unpack()
            result = None
            if interface == ITEM_IFACE:
                # With ItemIsMenu the host renders dbusmenu on primary click.
                # Explicit Activate is useful for hosts' secondary/open actions.
                if name in ('Activate', 'SecondaryActivate'):
                    self.on_action('open', None)
                # ContextMenu is handled by the host; scroll does not alter audio.
            elif name == 'GetLayout':
                result = v('(u(ia{sv}av))', (self._revision, self._layout(*args)))
            elif name == 'GetGroupProperties':
                ids, names = args
                ids = ids or list(self.model.items)
                result = v('(a(ia{sv}))', ([(item_id, self._props(self.model.items[item_id], names))
                            for item_id in ids if item_id in self.model.items],))
            elif name == 'GetProperty':
                item_id, prop = args
                result = v('(v)', (self._props(self.model.items[item_id])[prop],))
            elif name == 'Event':
                self._dispatch(args[0], args[1])
            elif name == 'EventGroup':
                errors = []
                for item_id, event, _data, _timestamp in args[0]:
                    if item_id not in self.model.items:
                        errors.append(item_id)
                    else:
                        self._dispatch(item_id, event)
                result = v('(ai)', (errors,))
            elif name == 'AboutToShow':
                if args[0] not in self.model.items:
                    raise KeyError(args[0])
                result = v('(b)', (False,))
            elif name == 'AboutToShowGroup':
                result = v('(aiai)', ([], [item_id for item_id in args[0]
                                           if item_id not in self.model.items]))
            else:
                invocation.return_dbus_error('org.freedesktop.DBus.Error.UnknownMethod',
                                             'Método no disponible.')
                return
            invocation.return_value(result)
        except (KeyError, TypeError, ValueError):
            invocation.return_dbus_error('com.canonical.dbusmenu.Error.InvalidMenuItem',
                                         'Elemento de menú no disponible.')
        except Exception:
            logging.exception('Error al procesar una acción del indicador')
            invocation.return_dbus_error('org.freedesktop.DBus.Error.Failed',
                                         'No se pudo completar la acción.')
