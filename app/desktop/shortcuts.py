# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Opt-in desktop portal shortcuts. No keyboard capture or default key grabs."""
from uuid import uuid4
from xml.etree import ElementTree

APP_ID = 'io.github.shurecontrol.Linux'
DESTINATION = 'org.freedesktop.portal.Desktop'
PATH = '/org/freedesktop/portal/desktop'
INTERFACE = 'org.freedesktop.portal.GlobalShortcuts'
REGISTRY = 'org.freedesktop.host.portal.Registry'
ACTIONS = {
    'mute': 'Activar o silenciar el Shure MV7',
    'open': 'Abrir Shure Control Linux',
    'profile-next': 'Seleccionar el siguiente perfil de audio',
}
FALLBACK = 'Puedes crear atajos personalizados en Configuración de Ubuntu; consulta docs/ATAJOS.md.'


class PortalShortcuts:
    """Call start to inspect support; only configure may show the portal UI.

    on_action receives one of ACTIONS' IDs; on_status receives readable text.
    All callbacks run on the GLib main context. Sessions are explicitly closed.
    """

    def __init__(self, on_action, on_status):
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.on_action, self.on_status = on_action, on_status
        self.connection = None
        self.available = False
        self.active = False
        self.version = 0
        self.session = None
        self._subscriptions = []
        self._requests = {}
        self._generation = 0
        self._registry = False
        self._registered = False
        self._configuring = False
        self._detecting = False
        self._parent_window = ''
        self.status = 'Atajos globales desactivados.'

    def _status(self, text):
        self.status = text
        self.on_status(text)

    def start(self):
        if self.connection is not None:
            return
        try:
            address = self.Gio.dbus_address_get_for_bus_sync(self.Gio.BusType.SESSION, None)
            flags = (self.Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
                     self.Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
            # A dedicated connection owns this portal session and host app ID.
            self.connection = self.Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
            self.connection.set_exit_on_close(False)
            self.connection.connect('closed', self._connection_closed)
            self._detecting = True
            for interface, signal, callback in (
                ('org.freedesktop.portal.Request', 'Response', self._response),
                (INTERFACE, 'Activated', self._activated),
                (INTERFACE, 'ShortcutsChanged', self._changed),
                ('org.freedesktop.portal.Session', 'Closed', self._session_closed),
            ):
                self._subscriptions.append(self.connection.signal_subscribe(
                    DESTINATION, interface, signal, None, None,
                    self.Gio.DBusSignalFlags.NONE, callback, None))
            self._call('org.freedesktop.DBus.Introspectable', 'Introspect', None,
                       self._support, expected='(s)')
        except Exception as exc:
            self.stop()
            self._status('No se pudo consultar el portal de atajos: ' + str(exc) + '. ' + FALLBACK)

    def _support(self, value):
        try:
            interfaces = ElementTree.fromstring(value[0]).findall('interface')
            names = {item.attrib['name'] for item in interfaces}
            self.available = INTERFACE in names
            self._registry = REGISTRY in names
        except (ElementTree.ParseError, KeyError, TypeError):
            self.available = False
        if not self.available:
            self._detecting = False
            self._configuring = False
            self._status('El escritorio no ofrece el portal de atajos globales. ' + FALLBACK)
            return
        self._call('org.freedesktop.DBus.Properties', 'Get',
                   self.GLib.Variant('(ss)', (INTERFACE, 'version')), self._version, expected='(v)')

    def _version(self, value):
        self._detecting = False
        self.version = int(value[0])
        self._status('Portal disponible. Pulsa Configurar atajos para activarlos en esta sesión. ' + FALLBACK)
        if self._configuring:
            self._create_session()

    def configure(self, parent_window=''):
        """Explicit user action: may open the system's shortcut dialog."""
        if self._configuring:
            self._status('Ya hay una solicitud de configuración de atajos pendiente.')
            return
        self._parent_window = parent_window
        if self.session and self.active and self.version >= 2:
            self._call(INTERFACE, 'ConfigureShortcuts',
                       self.GLib.Variant('(osa{sv})', (self.session, parent_window, {})),
                       lambda _result: self._status('Configuración de atajos abierta en el escritorio.'))
            return
        self._configuring = True
        if self.connection is None:
            self.start()
        elif self.available and self.version:
            self._create_session()
        elif not self._detecting:
            self._detecting = True
            self._call('org.freedesktop.DBus.Introspectable', 'Introspect', None,
                       self._support, expected='(s)')

    def _create_session(self):
        self._close_session()
        if self._registry and not self._registered:
            self._call(REGISTRY, 'Register', self.GLib.Variant('(sa{sv})', (APP_ID, {})),
                       self._registration_done)
            return
        token = 'shure_' + uuid4().hex
        options = {'session_handle_token': self.GLib.Variant('s', token)}
        self._request('CreateSession', '(a{sv})', (), options, self._created)

    def _registration_done(self, _value):
        self._registered = True
        self._create_session()

    def _created(self, response, results):
        if response != 0 or not results.get('session_handle'):
            self._failed('El portal no creó la sesión de atajos o la solicitud fue cancelada.')
            return
        self.session = results['session_handle']
        shortcuts = [(key, {'description': self.GLib.Variant('s', label)}) for key, label in ACTIONS.items()]
        self._request('BindShortcuts', '(oa(sa{sv})sa{sv})',
                      (self.session, shortcuts, self._parent_window), {}, self._bound)

    def _bound(self, response, results):
        self._configuring = False
        if response != 0:
            self._failed('Configuración de atajos cancelada o rechazada por el escritorio.')
            return
        self._show_bindings(results.get('shortcuts', []))

    def _show_bindings(self, bindings):
        accepted = [(key, properties) for key, properties in bindings if key in ACTIONS]
        self.active = bool(accepted)
        if not accepted:
            self._status('El escritorio no asignó atajos. ' + FALLBACK)
            return
        text = '; '.join(f'{ACTIONS[key]}: {properties.get("trigger_description") or "configurado en el escritorio"}'
                         for key, properties in accepted)
        self._status('Atajos activos en esta sesión. ' + text)

    def _call(self, interface, method, parameters, callback, *, expected=None):
        generation = self._generation

        def done(connection, result, _data):
            try:
                value = connection.call_finish(result)
                if generation == self._generation:
                    callback(value.unpack() if value is not None else ())
            except Exception as exc:
                if generation == self._generation:
                    self._failed('El portal de atajos no completó la operación: ' + str(exc))

        self.connection.call(DESTINATION, PATH, interface, method, parameters,
                             self.GLib.VariantType.new(expected) if expected else None,
                             self.Gio.DBusCallFlags.NONE, 10000, None, done, None)

    def _request(self, method, signature, prefix, options, callback):
        token = 'shure_' + uuid4().hex
        sender = self.connection.get_unique_name()[1:].replace('.', '_')
        path = f'/org/freedesktop/portal/desktop/request/{sender}/{token}'
        options = {**options, 'handle_token': self.GLib.Variant('s', token)}
        timer = self.GLib.timeout_add_seconds(120, self._request_timeout, path)
        self._requests[path] = (callback, timer)

        def received(value):
            returned = value[0]
            if returned != path and path in self._requests:
                saved_callback, old_timer = self._requests.pop(path)
                self.GLib.source_remove(old_timer)
                new_timer = self.GLib.timeout_add_seconds(120, self._request_timeout, returned)
                self._requests[returned] = (saved_callback, new_timer)

        self._call(INTERFACE, method, self.GLib.Variant(signature, (*prefix, options)), received, expected='(o)')

    def _response(self, _connection, _sender, path, _interface, _signal, parameters, _data):
        pending = self._requests.pop(path, None)
        if pending is None:
            return
        callback, timer = pending
        self.GLib.source_remove(timer)
        response, results = parameters.unpack()
        callback(response, results)

    def _request_timeout(self, path):
        self._requests.pop(path, None)
        self._close_remote(path, 'org.freedesktop.portal.Request')
        self._failed('La configuración de atajos no terminó a tiempo. Puedes intentarlo de nuevo.')
        return False

    def _activated(self, _connection, _sender, _path, _interface, _signal, parameters, _data):
        session, action, _timestamp, _options = parameters.unpack()
        if self.active and session == self.session and action in ACTIONS:
            self.on_action(action)

    def _changed(self, _connection, _sender, _path, _interface, _signal, parameters, _data):
        session, shortcuts = parameters.unpack()
        if session == self.session:
            self._show_bindings(shortcuts)

    def _session_closed(self, _connection, _sender, path, _interface, _signal, _parameters, _data):
        if path == self.session:
            self.session = None
            self.active = False
            self._configuring = False
            self._status('El escritorio cerró la sesión de atajos. Pulsa Configurar para volver a activarlos.')

    def _connection_closed(self, connection, _remote, _error):
        if connection is self.connection:
            self.stop()
            self._status('Se perdió la conexión con el portal de atajos. ' + FALLBACK)

    def _close_remote(self, path, interface):
        if self.connection is not None and not self.connection.is_closed():
            self.connection.call(DESTINATION, path, interface, 'Close', None, None,
                                 self.Gio.DBusCallFlags.NONE, 1000, None, None, None)

    def _close_session(self):
        if self.session is not None:
            self._close_remote(self.session, 'org.freedesktop.portal.Session')
            self.session = None
        self.active = False

    def _failed(self, text):
        self._detecting = False
        for path, (_callback, timer) in list(self._requests.items()):
            self.GLib.source_remove(timer)
            self._close_remote(path, 'org.freedesktop.portal.Request')
        self._requests.clear()
        self._close_session()
        self._configuring = False
        self._status(text + ' ' + FALLBACK)

    def stop(self):
        self._generation += 1
        for path, (_callback, timer) in list(self._requests.items()):
            self.GLib.source_remove(timer)
            self._close_remote(path, 'org.freedesktop.portal.Request')
        self._requests.clear()
        self._close_session()
        connection, self.connection = self.connection, None
        if connection is not None:
            for subscription in self._subscriptions:
                connection.signal_unsubscribe(subscription)
            if not connection.is_closed():
                connection.close(None, None, None)
        self._subscriptions.clear()
        self.available = self.active = self._registered = self._configuring = False
        self._detecting = False
        self.version = 0
