# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Single-instance GTK application with optional resident lifecycle."""
import json
import logging
import signal
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib

from app.controllers.runtime import ControllerRuntime
from app.core import ControlError
from app.desktop.settings import DesktopSettings, Autostart
from app.desktop.indicator import Indicator
from app.desktop.shortcuts import PortalShortcuts


class ShureApplication(Adw.Application):
    def __init__(self, screenshot=None, runtime=None, desktop_settings=None):
        super().__init__(application_id='io.github.shurecontrol.Linux',
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.screenshot = screenshot
        self.runtime = runtime
        self.desktop_settings = desktop_settings or DesktopSettings()
        self.settings = self.desktop_settings.load()
        self.autostart = Autostart()
        self.resident = False
        self.indicator = None
        self.indicator_available = False
        self.shortcuts = None
        self.shortcut_status = 'Atajos opcionales; sin teclas reservadas.'
        self.window = None
        self.started = False
        self.quitting = False
        self.quit_pending = False
        self.quit_hold = False
        self.signal_ids = []
        self.connect('activate', self.on_activate)
        self.connect('command-line', self.on_command_line)
        self.connect('shutdown', self.on_shutdown)
        for name, callback in [('quit', self.request_quit), ('show', self.show_window),
                               ('toggle-mute', lambda: self.perform('mute')),
                               ('next-profile', lambda: self.perform('profile-next'))]:
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', lambda _action, _value, cb=callback: cb())
            self.add_action(action)
        self.set_accels_for_action('app.quit', ['<Primary>q'])

    def ensure_started(self, allow_resident=True):
        if self.started:
            return
        self.started = True
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        self.runtime = self.runtime or ControllerRuntime()
        self.runtime.listeners.append(self.on_state)
        self.runtime.message_listeners.append(self.on_message)
        self.indicator = Indicator(self, self.perform, self.on_indicator_availability)
        self.shortcuts = PortalShortcuts(self.perform, self.on_shortcut_status)
        self.shortcuts.start()
        self.runtime.start()
        if allow_resident and self.settings['resident_enabled']:
            self.set_resident(True, persist=False)
        self.signal_ids = [GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self.signal_quit)
                           for sig in (signal.SIGTERM, signal.SIGINT)]

    def signal_quit(self):
        self.request_quit()
        return True

    def on_activate(self, _app):
        self.show_window()

    def show_window(self, page=None):
        self.ensure_started()
        if self.window is None or self.window.closed:
            from app.ui.window import Window
            self.window = Window(self, self.screenshot)
        self.window.present()
        self.window.receive_state(self.runtime)
        if page:
            self.window.stack.set_visible_child_name(page)
        return self.window

    def on_command_line(self, _app, command_line):
        args = command_line.get_arguments()[1:]
        ephemeral_status = not self.started and '--status' in args
        ephemeral_restore = not self.started and '--restore-audio' in args
        if not self.started and '--quit' in args:
            return 0
        self.ensure_started(allow_resident=not (ephemeral_status or ephemeral_restore))
        if '--quit' in args:
            self.request_quit()
        elif '--status' in args:
            # A short nested GLib loop lets the serialized worker supply a fresh
            # snapshot before the remote command returns its output.
            self.hold()
            self.runtime.refresh()
            context = GLib.MainContext.default()
            while self.runtime.busy:
                context.iteration(True)
            snapshot = self.runtime.snapshot(self.resident)
            if snapshot['hardware']:
                snapshot['hardware'] = {k: v for k, v in snapshot['hardware'].items() if k != 'serial'}
            command_line.print_literal(json.dumps(snapshot, ensure_ascii=False) + '\n')
            if ephemeral_status:
                self.finish_quit()
            self.release()
        elif '--restore-audio' in args:
            outcome = {}
            def restore():
                try:
                    self.runtime.audio.restore()
                    outcome['ok'] = True
                except Exception as exc:
                    outcome['error'] = str(exc)
                    raise
            self.hold()
            accepted = self.runtime.work('Audio restaurado', restore)
            if accepted:
                context = GLib.MainContext.default()
                while self.runtime.busy:
                    context.iteration(True)
            if outcome.get('ok'):
                command_line.print_literal('Configuración de audio restaurada.\n')
            else:
                command_line.printerr_literal(outcome.get('error', 'Hay otra operación en curso.') + '\n')
            if ephemeral_restore:
                self.finish_quit()
            self.release()
            return 0 if outcome.get('ok') else 1
        elif '--toggle-mute' in args:
            self.perform('mute')
            self.ensure_action_lifetime()
        elif '--next-profile' in args:
            self.perform('profile-next')
            self.ensure_action_lifetime()
        elif '--profile' in args:
            try:
                key = args[args.index('--profile') + 1]
            except IndexError:
                return 2
            self.perform('profile', key)
            self.ensure_action_lifetime()
        elif '--background' in args:
            self.set_resident(True, persist=False)
            GLib.timeout_add(1200, self.check_background_access)
        else:
            self.show_window()
        return 0

    def ensure_action_lifetime(self):
        if not self.resident and not self.window:
            # Shortcuts can work without a pre-existing resident. One lightweight
            # instance holds only until its action/readback is finished.
            self.hold()
            def finish():
                if self.runtime.busy:
                    return True
                self.release()
                self.request_quit()
                return False
            GLib.timeout_add(150, finish)

    def check_background_access(self):
        if self.resident and not self.indicator_available:
            self.show_window('preferences')
            self.on_message('GNOME no ofrece un indicador disponible. La ventana y el lanzador siguen accesibles; no se instalaron extensiones.')
        return False

    def set_resident(self, enabled, persist=True):
        self.ensure_started()
        if persist:
            self.settings = self.desktop_settings.update(resident_enabled=enabled)
        if enabled == self.resident:
            return
        if enabled:
            self.hold()
            self.resident = True
            self.indicator.start()
        else:
            if not self.window or not self.window.get_visible():
                self.show_window()
            self.resident = False
            self.indicator.stop()
            self.indicator_available = False
            self.release()
        self.on_state(self.runtime)

    def on_indicator_availability(self, available):
        previously = self.indicator_available
        self.indicator_available = available
        if previously and not available and self.resident and not self.quitting:
            self.runtime.stop_meter()
            self.show_window('preferences')
            self.on_message('El indicador dejó de estar disponible. La ventana vuelve a estar accesible.')
        if self.window and not self.window.closed:
            self.window.update_desktop_preferences()

    def on_shortcut_status(self, message):
        self.shortcut_status = message
        if self.window and not self.window.closed:
            self.window.update_desktop_preferences()

    def configure_shortcuts(self):
        self.settings = self.desktop_settings.update(shortcuts_enabled=True)
        self.shortcuts.configure(parent_window='')

    def disable_shortcuts(self):
        self.shortcuts.stop()
        self.settings = self.desktop_settings.update(shortcuts_enabled=False)
        self.shortcut_status = 'Atajos del portal desactivados.'
        if self.window:
            self.window.update_desktop_preferences()

    def on_state(self, runtime):
        if self.indicator and self.resident:
            self.indicator.update(runtime.snapshot(True))

    def on_message(self, message):
        if message is None:
            self.withdraw_notification('operation')
            if self.window and not self.window.closed:
                self.window.show_message(None)
            return
        if self.window and self.window.get_visible() and not self.window.closed:
            self.withdraw_notification('operation')
            self.window.show_message(message)
        elif not self.quitting:
            notification = Gio.Notification.new('Shure Control Linux')
            notification.set_body(message[:260])
            notification.set_default_action('app.show')
            self.send_notification('operation', notification)

    def perform(self, action, value=None):
        self.ensure_started()
        if self.quitting:
            return
        if action == 'open':
            self.show_window()
        elif action == 'preferences':
            self.show_window('preferences')
        elif action == 'quit':
            self.request_quit()
        elif action == 'mute':
            self.runtime.toggle_mute()
        elif action == 'gain':
            self.runtime.adjust_gain(value)
        elif action == 'aec':
            self.runtime.toggle_aec()
        elif action == 'profile':
            self.runtime.apply_profile(value)
        elif action == 'profile-next':
            profiles = self.runtime.profiles.all()
            keys = [p['id'] for p in profiles if p['id'] != 'custom' or p['customized']]
            previous = self.runtime.last_profile
            next_index = (keys.index(previous) + 1) % len(keys) if previous in keys else 0
            self.runtime.apply_profile(keys[next_index])
        elif action == 'measure':
            self.runtime.start_measurement()

    def close_window(self, window):
        if self.quitting:
            return False
        if self.runtime.action_busy:
            self.on_message('Espera a que termine la operación antes de cerrar.')
            return True
        behavior = self.settings['close_behavior']
        if behavior == 'quit':
            self.request_quit()
        elif behavior in ('hide', 'resident'):
            self.background_window(window, destroy=behavior == 'resident')
        else:
            dialog = Adw.AlertDialog(heading='¿Cerrar Shure Control Linux?',
                                    body='Puedes salir completamente o mantener los controles rápidos en el indicador. La captura y las visualizaciones se detendrán al ocultar la ventana.')
            dialog.add_response('cancel', 'Cancelar')
            dialog.add_response('quit', 'Salir completamente')
            dialog.add_response('hide', 'Minimizar al indicador')
            dialog.set_default_response('hide' if self.resident else 'quit')
            dialog.set_close_response('cancel')
            def response(_dialog, answer):
                if answer == 'quit':
                    self.request_quit()
                elif answer == 'hide':
                    self.background_window(window)
            dialog.connect('response', response)
            dialog.present(window)
        return True

    def background_window(self, window, destroy=False):
        self.set_resident(True, persist=True)
        def hide():
            if self.indicator_available:
                window.pause_visuals()
                if destroy:
                    window.dispose_window()
                    window.destroy()
                    self.window = None
                else:
                    window.set_visible(False)
            else:
                self.show_window('preferences')
                self.on_message('No hay soporte de indicadores disponible. Usa el lanzador o los atajos; la ventana permanece accesible.')
            return False
        if self.indicator_available:
            hide()
        else:
            GLib.timeout_add(500, hide)

    def request_quit(self):
        if self.quitting or self.quit_pending:
            return
        self.ensure_started()
        self.quit_pending = True
        self.hold()
        self.quit_hold = True
        def wait_for_worker():
            if self.runtime.busy:
                return True
            if not self.settings['keep_aec_on_exit'] and self.runtime.audio.journal.exists():
                # Reuse reversible session administration. Refuse to exit on a
                # restoration failure so the user can resolve changed routes.
                self.runtime.work('Audio restaurado antes de salir', self.runtime.audio.restore,
                                  lambda _result: self.finish_quit())
                def restore_finished():
                    if self.runtime.busy:
                        return True
                    if not self.quitting:
                        self.quit_pending = False
                        self.show_window('aec')
                        if self.quit_hold:
                            self.release()
                            self.quit_hold = False
                    return False
                GLib.timeout_add(150, restore_finished)
            else:
                self.finish_quit()
            return False
        GLib.timeout_add(100, wait_for_worker)

    def finish_quit(self):
        self.quit_pending = False
        self.quitting = True
        self.withdraw_notification('operation')
        if self.window and not self.window.closed:
            self.window.dispose_window()
            self.window.destroy()
            self.window = None
        if self.indicator:
            self.indicator.stop()
        if self.shortcuts:
            self.shortcuts.stop()
        self.runtime.shutdown()
        for source_id in self.signal_ids:
            GLib.source_remove(source_id)
        self.signal_ids = []
        if self.quit_hold:
            self.release()
            self.quit_hold = False
        if self.resident:
            self.resident = False
            self.release()
        self.quit()

    def on_shutdown(self, _app):
        if self.runtime and not self.runtime.closed and not self.runtime.busy:
            self.runtime.shutdown()
        if self.indicator:
            self.indicator.stop()
        if self.shortcuts:
            self.shortcuts.stop()
