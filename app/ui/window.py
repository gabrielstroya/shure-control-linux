# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""GTK4/libadwaita UI. All device I/O is serialized off the GTK thread."""
import datetime
import json
import logging
from pathlib import Path
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, Gtk

from app.ui.visualizer import AudioVisualizer
from app.audio.pipewire import PipeWire
from app.controllers.shure import ShureController
from app.core import ControlError, config_dir, read_json, save_json, state_dir
from app.diagnostics.report import report, export
from app.i18n import _
from app import __version__
from app.profiles.store import Profiles, PRESETS


class Window(Adw.ApplicationWindow):
    def __init__(self, app, screenshot=None):
        super().__init__(application=app, title='Shure Control Linux', default_width=980, default_height=820)
        self.runtime = app.runtime
        self.closed = False
        self.meter_id = 0
        self.desktop_sync = False
        self.editing = False
        self.edit_base = None
        self.source_names = []
        self.sink_names = []
        self.controls = {}
        self.device_controls = []
        self.reading = False
        self.screenshot_path = screenshot
        self.screenshot_done = False
        self.connect('close-request', self.on_close_request)
        self.toast = Adw.ToastOverlay()
        self.active_toast = None
        self.set_content(self.toast)
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toast.set_child(layout)
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title='Shure Control Linux', subtitle='MV7 · Audio local'))
        self.refresh_button = Gtk.Button(icon_name='view-refresh-symbolic', tooltip_text=_('Actualizar dispositivos'))
        self.refresh_button.connect('clicked', lambda _b: self.manual_refresh())
        header.pack_end(self.refresh_button)
        layout.append(header)
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        switcher.set_halign(Gtk.Align.CENTER)
        layout.append(switcher)
        layout.append(self.stack)
        self.stack.set_vexpand(True)
        self.pages = {}
        for name, title, icon in [('main', 'Micrófono', 'audio-input-microphone-symbolic'),
                                   ('dsp', 'Procesamiento', 'preferences-system-symbolic'),
                                   ('aec', 'Eco y rutas', 'audio-speakers-symbolic'),
                                   ('profiles', 'Perfiles', 'document-properties-symbolic'),
                                   ('diagnostics', 'Diagnóstico', 'dialog-information-symbolic'),
                                   ('preferences', 'Preferencias', 'emblem-system-symbolic')]:
            page = Adw.PreferencesPage()
            self.pages[name] = page
            self.stack.add_titled_with_icon(page, name, _(title), icon)
        self.build_main()
        self.build_dsp()
        self.build_aec()
        self.build_profiles()
        self.build_diagnostics()
        self.build_preferences()
        self.status = Gtk.Label(label=_('Leyendo dispositivos…'), wrap=True, xalign=0)
        self.status.set_margin_start(20)
        self.status.set_margin_end(20)
        self.status.set_margin_top(8)
        self.status.set_margin_bottom(12)
        layout.append(self.status)
        self.runtime.listeners.append(self.receive_state)
        self.connect('notify::visible', self.visibility_changed)
        self.stack.connect('notify::visible-child-name', self.visibility_changed)
        if self.find_property('suspended'):
            self.connect('notify::suspended', self.visibility_changed)
        self.update_desktop_preferences()

    def group(self, page, title, description=None):
        group = Adw.PreferencesGroup(title=_(title))
        if description:
            group.set_description(_(description))
        self.pages[page].add(group)
        return group

    def row(self, group, title, subtitle=None):
        row = Adw.ActionRow(title=_(title))
        if subtitle:
            row.set_subtitle(_(subtitle))
        row.set_title_lines(2)
        row.set_subtitle_lines(3)
        group.add(row)
        return row

    def button(self, row, label, callback, style=None):
        button = Gtk.Button(label=_(label), valign=Gtk.Align.CENTER)
        if style:
            button.add_css_class(style)
        button.connect('clicked', lambda _b: callback())
        row.add_suffix(button)
        return button

    def scale(self, group, title, low, high, step, key=None, subtitle=None):
        row = self.row(group, title, subtitle)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, low, high, step)
        scale.set_digits(1 if step == 1.5 else 0)
        scale.set_draw_value(True)
        scale.set_size_request(240, -1)
        scale.set_valign(Gtk.Align.CENTER)
        row.add_suffix(scale)
        if key:
            self.controls[key] = scale
            self.device_controls.append(scale)
        return scale

    def combo(self, group, title, options, key=None):
        combo = Adw.ComboRow(title=_(title), model=Gtk.StringList.new([_(label) for value, label in options]))
        combo.options = [value for value, _ in options]
        group.add(combo)
        if key:
            self.controls[key] = combo
            self.device_controls.append(combo)
        return combo

    def build_main(self):
        group = self.group('main', 'Shure MV7', 'Control USB del micrófono original')
        self.connection_row = self.row(group, 'Conexión', 'Comprobando acceso HID…')
        self.mute_row = self.row(group, 'Mute del micrófono', 'Estado desconocido')
        self.mute_button = self.button(self.mute_row, 'Silenciar', self.toggle_mute, 'destructive-action')
        self.device_controls.append(self.mute_button)
        self.mode = self.combo(group, 'Modo', [('manual', 'Manual'), ('auto', 'Automático')], 'mode')
        self.mode.connect('notify::selected', self.mode_changed)
        self.gain = self.scale(group, 'Ganancia interna · dB', 0, 36, 1.5, 'gain_db', 'DSP del MV7 · pasos de 1,5 dB')
        self.gain_button = self.button(self.row(group, 'Ajustes de entrada'), 'Aplicar modo y ganancia', self.apply_main, 'suggested-action')
        self.device_controls.append(self.gain_button)
        self.button(self.row(group, 'Último cambio del micrófono'), 'Deshacer', self.undo_hardware)
        meter = self.group('main', 'Nivel de entrada', 'Medición posterior al volumen PipeWire; audio solo en memoria.')
        self.level_row = self.row(meter, 'Medidor detenido', 'Actívalo para medir la entrada física seleccionada.')
        self.level = Gtk.LevelBar(min_value=0, max_value=1)
        self.level.set_size_request(260, 16)
        self.level.set_valign(Gtk.Align.CENTER)
        self.level_row.add_suffix(self.level)
        self.meter_button = self.button(self.row(meter, 'Captura temporal'), 'Iniciar medidor', self.toggle_meter)
        self.visualizer = AudioVisualizer()
        meter.add(self.visualizer)
        self.sample_row = self.row(meter, 'Formato y frecuencia', '—')
        group = self.group('main', 'Volumen del sistema', 'Este volumen es independiente de la ganancia interna del Shure.')
        self.os_volume = self.scale(group, 'Entrada física · %', 0, 100, 1)
        self.button(self.row(group, 'Volumen PipeWire'), 'Aplicar volumen', self.apply_volume)
        self.default_row = self.row(group, 'Entrada predeterminada', '—')
        self.main_aec = self.row(group, 'Cancelación de eco', '—')

    def build_dsp(self):
        group = self.group('dsp', 'Procesamiento del MV7', 'Los cambios se aplican explícitamente y se leen de nuevo desde el dispositivo.')
        for key, title, options in [
            ('eq_preset', 'Ecualizador · modo manual', [('flat', 'Plano'), ('high_pass', 'Paso alto'), ('presence_boost', 'Presencia'), ('high_pass_presence_boost', 'Paso alto + presencia')]),
            ('compressor', 'Compresor · modo manual', [('off', 'Desactivado'), ('light', 'Ligero'), ('medium', 'Medio'), ('heavy', 'Intenso')]),
            ('auto_position', 'Posición · modo automático', [('near', 'Near · cerca'), ('far', 'Far · lejos')]),
            ('auto_tone', 'Tono · modo automático', [('dark', 'Cálido'), ('natural', 'Natural'), ('bright', 'Brillante')]),
        ]:
            self.combo(group, title, options, key)
        limiter = Adw.SwitchRow(title=_('Limitador · modo manual'), subtitle=_('Solo disponible si el dispositivo devuelve este parámetro.'))
        group.add(limiter)
        self.controls['limiter_enabled'] = limiter
        self.device_controls.append(limiter)
        self.scale(group, 'Monitor Mix · % reproducción', 0, 100, 1, 'monitor_mix', '0 = micrófono · 100 = reproducción USB')
        button = self.button(self.row(group, 'Aplicar al micrófono'), 'Aplicar procesamiento', self.apply_dsp, 'suggested-action')
        self.device_controls.append(button)
        group = self.group('dsp', 'Disponibilidad')
        self.row(group, 'Volumen de auriculares independiente', 'No expuesto por shurectl para el MV7 original. Monitor Mix cambia el balance, no el volumen general.')
        self.row(group, 'EQ paramétrico, reducción de ruido y reverb', 'No compatibles con este modelo mediante el controlador integrado.')

    def build_aec(self):
        group = self.group('aec', 'Rutas físicas', 'WebRTC AEC necesita que el micrófono y la reproducción pasen por la misma ruta virtual.')
        self.source_combo = Adw.ComboRow(title=_('Micrófono físico'), model=Gtk.StringList.new([]))
        self.sink_combo = Adw.ComboRow(title=_('Altavoces físicos'), model=Gtk.StringList.new([]))
        group.add(self.source_combo)
        group.add(self.sink_combo)
        self.aec_row = self.row(group, 'Estado AEC', '—')
        self.button(self.row(group, 'Nueva sesión de cancelación', 'No crea un segundo módulo si ya existe uno.'), 'Activar AEC', lambda: self.enable_aec(False), 'suggested-action')
        self.button(self.row(group, 'AEC creado anteriormente', 'Asume su administración y guarda una copia para restaurarlo.'), 'Administrar AEC existente', lambda: self.enable_aec(True))
        self.button(self.row(group, 'Detener cancelación administrada'), 'Desactivar AEC', lambda: self.work('AEC desactivado', self.audio.disable))
        self.button(self.row(group, 'Aplicaciones que ya están abiertas', 'Mueve solo flujos de las entradas/salidas físicas seleccionadas. Puede interrumpir brevemente el audio.'), 'Enrutar flujos actuales', lambda: self.work('Rutas actualizadas', self.audio.route_streams))
        self.button(self.row(group, 'Volver al estado anterior', 'Restaura predeterminados, módulos y movimientos hechos por esta aplicación.'), 'Restaurar audio', lambda: self.work('Audio restaurado', self.audio.restore))
        self.row(self.group('aec', 'Validación acústica'), 'Pendiente de prueba real', 'Un módulo activo no demuestra que el eco desaparezca. Verifica con otra persona en una llamada, con los altavoces que vayas a utilizar.')

    def build_profiles(self):
        group = self.group('profiles', 'Perfiles locales', 'Puntos de partida editables; no son optimizaciones acústicas certificadas. No modifican las aplicaciones de llamadas.')
        self.profile_combo = self.combo(group, 'Perfil', [(key, name) for key, name, aec, settings in PRESETS])
        self.profile_combo.connect('notify::selected', lambda *_: self.profile_preview())
        self.profile_description = self.row(group, 'Contenido del perfil', '')
        self.button(self.row(group, 'Guardar configuración real', 'Guarda parámetros compatibles y las rutas físicas elegidas. No guarda mute.'), 'Guardar en este perfil', self.save_profile)
        self.button(self.row(group, 'Aplicar perfil', 'Puede cambiar modo y AEC. Conserva el mute actual y permite deshacer los ajustes del micrófono.'), 'Aplicar perfil', self.apply_profile, 'suggested-action')
        self.row(group, 'Hugo / AmparoLex', 'Solo un nombre de perfil local. Sin conexión con Raspberry Pi, Hugo, AmparoLex ni servicios externos.')
        self.profile_preview()

    def build_diagnostics(self):
        group = self.group('diagnostics', 'Diagnóstico local', 'Se exportan estados y nombres de aplicaciones, nunca audio ni credenciales.')
        self.usb_row = self.row(group, 'USB y permisos', '—')
        self.clients_row = self.row(group, 'Procesos que capturan audio', '—')
        self.button(self.row(group, 'Informe técnico'), 'Exportar diagnóstico', self.export_report)
        self.button(self.row(group, 'Copia inicial del micrófono', 'Restaura los ajustes guardados antes del primer cambio; puede cambiar mute.'), 'Restaurar micrófono', self.restore_hardware)
        group = self.group('diagnostics', 'Aplicación')
        self.row(group, 'Shure Control Linux', __version__)
        self.row(group, 'Privacidad', 'Sin telemetría ni conexiones de red. El medidor se detiene al cerrar la ventana. Los cambios de audio solo se aplican con los botones.')
        self.row(group, 'Inicio de sesión', 'Inicio automático opcional desde Preferencias. Abrir la aplicación no cambia rutas ni aplica perfiles.')

    def build_preferences(self):
        group = self.group('preferences', 'Escritorio', 'El inicio automático y el modo residente son opcionales.')
        self.resident_switch = Adw.SwitchRow(title='Mantener control residente', subtitle='Indicador de estado y controles rápidos en el panel de GNOME.')
        group.add(self.resident_switch)
        self.resident_switch.connect('notify::active', self.resident_changed)
        self.indicator_row = self.row(group, 'Indicador', 'Comprobando disponibilidad…')
        self.close_combo = self.combo(group, 'Al cerrar la ventana', [('ask', 'Preguntar'), ('quit', 'Salir completamente'), ('hide', 'Minimizar al indicador'), ('resident', 'Cerrar ventana y mantener controlador')])
        self.close_combo.connect('notify::selected', self.close_behavior_changed)
        self.autostart_switch = Adw.SwitchRow(title='Iniciar con la sesión', subtitle='Entrada de inicio propia, sin servicios del sistema.')
        group.add(self.autostart_switch)
        self.autostart_switch.connect('notify::active', self.autostart_changed)
        self.keep_aec_switch = Adw.SwitchRow(title='Conservar AEC al salir', subtitle='PipeWire puede mantenerlo sin la aplicación. Desactiva para restaurar la sesión administrada antes de salir.')
        group.add(self.keep_aec_switch)
        self.keep_aec_switch.connect('notify::active', self.keep_aec_changed)
        group = self.group('preferences', 'Atajos globales', 'GNOME solicita permiso y permite elegir las teclas. No se asignan combinaciones automáticamente.')
        self.shortcuts_row = self.row(group, 'Mute, abrir ventana y siguiente perfil', 'Comprobando portal…')
        self.button(self.row(group, 'Permisos de teclado'), 'Configurar atajos', lambda: self.get_application().configure_shortcuts())
        self.button(self.row(group, 'Sesión de atajos'), 'Desactivar atajos', lambda: self.get_application().disable_shortcuts())
        self.row(group, 'Persistencia de los atajos', 'Este portal requiere configurar la sesión al volver a iniciar. También puedes asignar los comandos documentados en docs/MAINTENANCE.md desde los ajustes de GNOME.')
        self.button(self.row(self.group('preferences', 'Aplicación'), 'Detener ventana e indicador'), 'Salir completamente', lambda: self.get_application().request_quit())

    def update_desktop_preferences(self):
        if not hasattr(self, 'resident_switch'):
            return
        app = self.get_application()
        self.desktop_sync = True
        self.resident_switch.set_active(app.resident)
        self.close_combo.set_selected(self.close_combo.options.index(app.settings['close_behavior']))
        try:
            self.autostart_switch.set_active(app.autostart.is_enabled())
        except ControlError as exc:
            self.autostart_switch.set_active(False)
            self.autostart_switch.set_subtitle(str(exc))
        self.keep_aec_switch.set_active(app.settings['keep_aec_on_exit'])
        self.indicator_row.set_subtitle('Disponible en GNOME' if app.indicator_available else 'Desactivado' if not app.resident else 'Sin anfitrión disponible; ventana accesible')
        self.shortcuts_row.set_subtitle(getattr(app, 'shortcut_status', 'Comprobando portal…'))
        self.desktop_sync = False

    def resident_changed(self, widget, *_):
        if self.desktop_sync:
            return
        app = self.get_application()
        try:
            if not widget.get_active() and app.autostart.is_enabled():
                app.autostart.set_enabled(False)
                app.settings = app.desktop_settings.update(autostart=False)
            app.set_resident(widget.get_active())
        except (ControlError, OSError) as exc:
            self.notify(str(exc))
        self.update_desktop_preferences()

    def close_behavior_changed(self, widget, *_):
        if not self.desktop_sync:
            app = self.get_application()
            app.settings = app.desktop_settings.update(close_behavior=widget.options[widget.get_selected()])

    def autostart_changed(self, widget, *_):
        if self.desktop_sync:
            return
        app = self.get_application()
        try:
            app.autostart.set_enabled(widget.get_active())
            app.settings = app.desktop_settings.update(autostart=widget.get_active())
            if widget.get_active():
                app.set_resident(True)
        except (ControlError, OSError) as exc:
            self.notify(str(exc))
        self.update_desktop_preferences()

    def keep_aec_changed(self, widget, *_):
        if not self.desktop_sync:
            app = self.get_application()
            app.settings = app.desktop_settings.update(keep_aec_on_exit=widget.get_active())

    def values(self, keys):
        result = {}
        for key in keys:
            widget = self.controls[key]
            if isinstance(widget, Adw.ComboRow):
                result[key] = widget.options[widget.get_selected()]
            elif isinstance(widget, Adw.SwitchRow):
                result[key] = widget.get_active()
            else:
                value = widget.get_value()
                result[key] = round(value / 1.5) * 1.5 if key == 'gain_db' else int(value)
        return result

    def mode_changed(self, *_):
        self.mark_edit()
        if self.hardware:
            self.update_enabled()

    def update_enabled(self):
        ready = self.hardware is not None and not self.busy
        for widget in self.device_controls:
            widget.set_sensitive(ready)
        mode = (self.hardware or {}).get('settings', {}).get('mode', 'manual')
        desired_mode = self.mode.options[self.mode.get_selected()]
        for key in ('gain_db', 'eq_preset', 'compressor', 'limiter_enabled'):
            self.controls[key].set_sensitive(ready and (desired_mode if key == 'gain_db' else mode) == 'manual' and key in (self.hardware or {}).get('settings', {}))
        for key in ('auto_position', 'auto_tone'):
            self.controls[key].set_sensitive(ready and mode == 'auto')

    def selected_pair(self):
        s, k = self.source_combo.get_selected(), self.sink_combo.get_selected()
        if s >= len(self.source_names) or k >= len(self.sink_names):
            raise ControlError('Selecciona una entrada y una salida físicas disponibles.')
        return self.source_names[s], self.sink_names[k]

    def notify(self, message):
        self.runtime.notify(message)

    def show_message(self, message):
        if self.active_toast is not None:
            self.active_toast.dismiss()
            self.active_toast = None
        self.status.set_label(message or self.current_status())
        if message is not None:
            self.active_toast = Adw.Toast(title=message[:180], timeout=7)
            self.toast.add_toast(self.active_toast)

    def current_status(self):
        runtime = self.runtime
        if runtime.operation_error:
            return runtime.operation_error
        if runtime.last_message:
            return runtime.last_message
        if runtime.audio_error:
            return _('Estado de audio: ') + runtime.audio_error
        if runtime.hardware is None:
            return (_('Control USB no disponible; diagnóstico y rutas siguen accesibles.')
                    if runtime.updated_at else _('Leyendo dispositivos…'))
        if runtime.audio_data is not None:
            return _('MV7 conectado · ') + self.audio.status(runtime.audio_data)
        return _('MV7 conectado · estado de audio pendiente.')

    def work(self, message, task, on_result=None):
        def done(result):
            self.editing = False
            self.edit_base = None
            if on_result:
                on_result(result)
        return self.runtime.work(message, task, done)

    def hardware_change(self, patch):
        expected = self.edit_base or self.hardware
        def task():
            before = self.shure.read()
            result = self.shure.apply(patch, expected=expected)
            return before, result
        def done(result):
            self.previous_hardware, self.hardware = result
            self.editing = False
            self.edit_base = None
        self.work(_('Cambio confirmado por el MV7'), task, done)

    def toggle_mute(self):
        self.runtime.toggle_mute()

    def remember_hardware(self, result):
        self.previous_hardware, self.hardware = result
        self.editing = False
        self.edit_base = None

    def apply_main(self):
        patch = self.values(['mode'])
        if patch['mode'] == 'manual':
            patch.update(self.values(['gain_db']))
        self.hardware_change(patch)

    def apply_dsp(self):
        if not self.hardware:
            return
        mode = self.hardware['settings']['mode']
        keys = ['monitor_mix'] + (['eq_preset', 'compressor'] if mode == 'manual' else ['auto_position', 'auto_tone'])
        if mode == 'manual' and 'limiter_enabled' in self.hardware['settings']:
            keys.append('limiter_enabled')
        self.hardware_change(self.values(keys))

    def undo_hardware(self):
        if self.previous_hardware:
            snapshot = self.previous_hardware
            self.work(_('Último cambio deshecho'), lambda: self.shure.restore(snapshot), lambda _r: setattr(self, 'previous_hardware', None))
        else:
            self.notify(_('No hay un cambio anterior en esta sesión.'))

    def restore_hardware(self):
        try:
            snapshot = read_json(self.shure.state_path)
        except ControlError as exc:
            self.notify(str(exc))
            return
        if snapshot:
            self.work(_('Micrófono restaurado'), lambda: self.shure.restore(snapshot))
        else:
            self.notify(_('Todavía no hay una copia inicial del micrófono.'))

    def apply_volume(self):
        try:
            source, _sink = self.selected_pair()
            value = self.os_volume.get_value()
            self.work(_('Volumen PipeWire aplicado'), lambda: self.audio.set_volume('source', source, value))
        except ControlError as exc:
            self.notify(str(exc))

    def toggle_meter(self):
        if self.meter.process:
            self.pause_visuals()
        else:
            try:
                selected = self.source_combo.get_selected()
                if selected >= len(self.source_names):
                    raise ControlError('Selecciona una entrada física disponible.')
                source = self.source_names[selected]
                self.runtime.stop_meter()
                self.meter.start(source)
                if self.meter.error:
                    raise ControlError(self.meter.error)
                self.meter.visuals_enabled = True
                self.meter_button.set_label(_('Detener medidor'))
                self.meter_id = GLib.timeout_add(100, self.update_meter)
            except ControlError as exc:
                self.notify(str(exc))

    def pause_visuals(self):
        if self.meter_id:
            GLib.source_remove(self.meter_id)
            self.meter_id = 0
        self.runtime.stop_meter()
        self.visualizer.clear()
        self.level.set_value(0)
        self.level_row.set_title(_('Medidor detenido'))
        self.meter_button.set_label(_('Iniciar medidor'))

    def visibility_changed(self, *_):
        suspended = self.get_property('suspended') if self.find_property('suspended') else False
        if not self.get_visible() or self.stack.get_visible_child_name() != 'main' or suspended:
            self.pause_visuals()

    def update_meter(self):
        if self.closed or not self.get_visible() or not self.meter.process:
            self.meter_id = 0
            self.meter.visuals_enabled = False
            return False
        if self.meter.error:
            error = self.meter.error
            self.meter_id = 0
            self.pause_visuals()
            self.level_row.set_title(_('Medición no disponible'))
            self.level_row.set_subtitle(error[:180])
            return False
        else:
            self.level.set_value(max(0, min(1, (self.meter.peak + 60) / 60)))
            self.level_row.set_title(f'Pico {self.meter.peak:.1f} dBFS · RMS {self.meter.rms:.1f} dBFS')
            self.level_row.set_subtitle(_('Saturación detectada') if self.meter.clipping else self.meter.source)
            self.visualizer.set_data(self.meter.visual_snapshot())
        return True

    def enable_aec(self, adopt):
        try:
            source, sink = self.selected_pair()
            self.preferences.update(source=source, sink=sink)
            save_json(self.preferences_path, self.preferences)
            self.work(_('AEC activo; validación acústica pendiente'), lambda: self.audio.enable(source, sink, adopt=adopt))
        except ControlError as exc:
            self.notify(str(exc))

    def profile_key(self):
        return self.profile_combo.options[self.profile_combo.get_selected()]

    def profile_preview(self):
        if not hasattr(self, 'profile_description'):
            return
        try:
            profile = self.profiles.get(self.profile_key())
        except (ControlError, ValueError, TypeError) as exc:
            self.profile_description.set_subtitle(str(exc))
            return
        label = 'Personalizado' if profile['customized'] else 'Punto de partida'
        self.profile_description.set_subtitle(f"{label} · AEC {'sí' if profile['aec'] else 'no'} · " + ', '.join(f'{k}: {v}' for k, v in profile['settings'].items()))

    def save_profile(self):
        try:
            source, sink = self.selected_pair()
            key = self.profile_key()
            aec = bool(self.audio_data and self.audio.echo_modules(self.audio_data))
            self.work(_('Perfil guardado localmente'), lambda: self.profiles.save(key, self.shure.read(), source, sink, aec), lambda _r: self.profile_preview())
        except ControlError as exc:
            self.notify(str(exc))

    def apply_profile(self):
        try:
            profile = self.profiles.get(self.profile_key())
            source, sink = self.selected_pair() if profile['aec'] else (None, None)
            self.editing = False
            self.edit_base = None
            self.runtime.apply_profile(self.profile_key(), source, sink)
        except ControlError as exc:
            self.notify(str(exc))

    def export_report(self):
        timestamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        path = state_dir() / f'diagnostico-{timestamp}.json'
        self.work(_('Diagnóstico exportado'), lambda: export(path, report(self.audio, self.hardware, self.hardware_error)),
                  lambda result: self.notify(_('Guardado en: ') + result))

    def manual_refresh(self):
        self.editing = False
        self.edit_base = None
        self.refresh()

    def refresh(self):
        return self.runtime.refresh()

    def receive_state(self, runtime):
        if self.closed or not self.get_visible():
            return
        hardware, data, error = runtime.hardware, runtime.audio_data, runtime.hardware_error
        self.connection_row.set_subtitle(_('Conectado · firmware ') + hardware['firmware_version'] if hardware else (error or _('Sin conexión')))
        self.mute_row.set_subtitle(_('Silenciado · confirmado') if hardware and hardware['muted'] else _('Micrófono activo') if hardware else _('Desconocido · sin lectura'))
        self.mute_button.set_label(_('Activar micrófono') if hardware and hardware['muted'] else _('Silenciar'))
        self.usb_row.set_subtitle(', '.join(d['path'] + (' · accesible' if d['accessible'] else ' · sin permiso') for d in runtime.devices) or _('MV7 no detectado por USB'))
        if hardware and not self.editing:
            self.populate_hardware(hardware)
        if data:
            self.populate_audio(data)
        else:
            self.main_aec.set_subtitle(runtime.audio_error or 'Estado de audio desconocido')
            self.aec_row.set_subtitle('Estado desconocido · no se pudo consultar PipeWire')
            self.default_row.set_subtitle('Sin lectura actual')
            self.clients_row.set_subtitle('Sin lectura actual')
            self.sample_row.set_subtitle('Sin lectura actual')
        self.update_enabled()
        if runtime.updated_at:
            self.status.set_label(self.current_status())
        if self.screenshot_path and not self.screenshot_done and runtime.updated_at:
            self.screenshot_done = True
            GLib.timeout_add(1000, self.capture_screenshot)

    def populate_hardware(self, state):
        self.reading = True
        for key, widget in self.controls.items():
            if key not in state['settings']:
                continue
            value = state['settings'][key]
            if isinstance(widget, Adw.ComboRow):
                widget.set_selected(widget.options.index(value))
            elif isinstance(widget, Adw.SwitchRow):
                widget.set_active(value)
            else:
                widget.set_value(value)
            if not getattr(widget, 'tracks_edits', False):
                signal = 'notify::selected' if isinstance(widget, Adw.ComboRow) else 'notify::active' if isinstance(widget, Adw.SwitchRow) else 'value-changed'
                widget.connect(signal, lambda *_: self.mark_edit())
                widget.tracks_edits = True
        self.reading = False

    def mark_edit(self):
        if not self.reading:
            if not self.editing:
                self.edit_base = self.hardware
            self.editing = True

    @staticmethod
    def device_label(device):
        properties = device.get('properties', {})
        for label in (device.get('description'), properties.get('device.description'), properties.get('alsa.card_name')):
            if label and label != '(null)':
                return label
        return device['name']

    def populate_audio(self, data):
        for kind, widget, attr, preferred in [('sources', self.source_combo, 'source_names', 'Shure'), ('sinks', self.sink_combo, 'sink_names', 'hdmi')]:
            devices = self.audio.physical(data, kind)
            names = [d['name'] for d in devices]
            if names != getattr(self, attr):
                old_names = getattr(self, attr)
                selected = widget.get_selected()
                old = old_names[selected] if selected < len(old_names) else self.preferences.get('source' if kind == 'sources' else 'sink')
                setattr(self, attr, names)
                widget.set_model(Gtk.StringList.new([self.device_label(d) for d in devices]))
                target = old if old in names else next((n for n in names if preferred.lower() in n.lower()), names[0] if names else None)
                if target:
                    widget.set_selected(names.index(target))
        self.default_row.set_subtitle(data['default_source'])
        status = self.audio.status(data)
        self.aec_row.set_subtitle(status)
        self.main_aec.set_subtitle(status)
        if self.source_combo.get_selected() < len(self.source_names):
            name = self.source_names[self.source_combo.get_selected()]
            device = next(d for d in data['sources'] if d['name'] == name)
            self.sample_row.set_subtitle(device.get('sample_specification', '—'))
            if not getattr(self.os_volume, 'initialized', False):
                channels = list(device.get('volume', {}).values())
                if channels:
                    self.os_volume.set_value(min(100, int(channels[0]['value_percent'].strip('%'))))
                self.os_volume.initialized = True
        apps = [s.get('properties', {}).get('application.name', 'Desconocida') +
                ' · PID ' + str(s.get('properties', {}).get('application.process.id', '—')) for s in data['source-outputs'] if s.get('client') is not None]
        self.clients_row.set_subtitle(', '.join(apps) or _('Sin clientes de captura activos'))

    def capture_screenshot(self):
        try:
            paintable = Gtk.WidgetPaintable.new(self)
            snapshot = Gtk.Snapshot.new()
            paintable.snapshot(snapshot, self.get_width(), self.get_height())
            node = snapshot.to_node()
            if node:
                texture = self.get_native().get_renderer().render_texture(node, None)
                texture.save_to_png(self.screenshot_path)
        except Exception as exc:
            logging.warning('Captura UI: %s', str(exc))
        return False

    def on_close_request(self, *_):
        return self.get_application().close_window(self)

    def dispose_window(self):
        if self.closed:
            return
        self.closed = True
        if self.receive_state in self.runtime.listeners:
            self.runtime.listeners.remove(self.receive_state)
        self.pause_visuals()


# Compatibility for scripts importing the original application entry point.
from app.ui.application import ShureApplication


def runtime_property(name):
    return property(lambda self: getattr(self.runtime, name),
                    lambda self, value: setattr(self.runtime, name, value))


for _name in ('shure', 'audio', 'profiles', 'meter', 'executor', 'hardware', 'audio_data',
              'hardware_error', 'previous_hardware', 'preferences', 'preferences_path', 'poll_id'):
    setattr(Window, _name, runtime_property(_name))
Window.busy = property(lambda self: self.runtime.busy)
