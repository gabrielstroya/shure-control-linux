# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Compact native Cairo audio display, driven solely by its visible owner.

This widget does not capture audio, create a timer, or perform FFT calculations.
"""
import math

import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

from app.audio.meter import EMPTY_VISUAL, FLOOR_DB
from app.i18n import _


class AudioVisualizer(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self._snapshot = EMPTY_VISUAL
        self.set_content_height(220)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.set_tooltip_text(_('Espectro FFT y forma de onda de la entrada seleccionada. Audio solo en memoria.'))
        self.update_property([Gtk.AccessibleProperty.LABEL],
                             [_('Espectro de audio y forma de onda; niveles numéricos disponibles en el medidor.')])

    def set_data(self, snapshot):
        if snapshot is not self._snapshot:
            self._snapshot = snapshot or EMPTY_VISUAL
            self.queue_draw()

    def clear(self):
        self.set_data(EMPTY_VISUAL)

    @staticmethod
    def _text(context, label, x, y, size=10, muted=True):
        context.set_source_rgb(*(0.56, 0.63, 0.69) if muted else (0.86, 0.91, 0.94))
        context.set_font_size(size)
        context.move_to(x, y)
        context.show_text(label)

    def _draw(self, _area, context, width, height):
        if width < 100 or height < 100:
            return
        context.set_source_rgb(0.065, 0.082, 0.10)
        context.paint()
        left, right = 38, width - 14
        top, bottom = 28, height * 0.51
        plot_width, plot_height = right - left, bottom - top
        self._text(context, _('ESPECTRO FFT'), 14, 16, muted=False)
        self._text(context, 'dBFS', right - 30, 16)
        context.set_line_width(1)
        for level in (0, -30, -60, -90):
            y = top + -level / -FLOOR_DB * plot_height
            context.set_source_rgba(0.4, 0.52, 0.60, 0.17)
            context.move_to(left, y)
            context.line_to(right, y)
            context.stroke()
            self._text(context, str(level), 7, y + 3)

        log_start, log_span = math.log10(30), math.log10(20000 / 30)

        def x_at(frequency):
            return left + (math.log10(frequency) - log_start) / log_span * plot_width

        for frequency, label in ((100, '100 Hz'), (1000, '1 kHz'), (10000, '10 kHz')):
            x = x_at(frequency)
            context.set_source_rgba(0.4, 0.52, 0.60, 0.17)
            context.move_to(x, top)
            context.line_to(x, bottom)
            context.stroke()
            self._text(context, label, x - 13, bottom + 14)
        spectrum = self._snapshot.get('spectrum', ())
        frequencies = self._snapshot.get('frequencies', ())
        points = [(x_at(frequency), top + (0 - max(FLOOR_DB, min(0, magnitude))) / -FLOOR_DB * plot_height)
                  for frequency, magnitude in zip(frequencies, spectrum) if 30 <= frequency <= 20000]
        if points:
            context.move_to(points[0][0], bottom)
            for x, y in points:
                context.line_to(x, y)
            context.line_to(points[-1][0], bottom)
            context.close_path()
            context.set_source_rgba(0.12, 0.77, 0.83, 0.13)
            context.fill()
            context.move_to(*points[0])
            for x, y in points[1:]:
                context.line_to(x, y)
            context.set_source_rgb(0.25, 0.83, 0.87)
            context.set_line_width(1.4)
            context.stroke()

        wave_top, wave_bottom = bottom + 44, height - 13
        wave_center = (wave_top + wave_bottom) / 2
        self._text(context, _('FORMA DE ONDA'), 14, wave_top - 10, muted=False)
        context.set_source_rgba(0.4, 0.52, 0.60, 0.22)
        context.set_line_width(1)
        context.move_to(left, wave_center)
        context.line_to(right, wave_center)
        context.stroke()
        waveform = self._snapshot.get('waveform', ())
        if waveform:
            half_height = max(1, (wave_bottom - wave_top) / 2)
            for index, value in enumerate(waveform):
                x = left + index / max(1, len(waveform) - 1) * plot_width
                y = wave_center - max(-1, min(1, value)) * half_height
                if index == 0:
                    context.move_to(x, y)
                else:
                    context.line_to(x, y)
            context.set_source_rgb(0.47, 0.86, 0.67)
            context.set_line_width(1.2)
            context.stroke()
        else:
            self._text(context, _('Sin medición activa'), left + 8, wave_center - 6)
