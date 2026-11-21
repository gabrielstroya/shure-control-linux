# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Transient metering and optional visual analysis of one in-memory PCM stream."""
import array
from collections import deque
import math
import os
import subprocess
import sys
import threading
from types import MappingProxyType


SAMPLE_RATE = 48000
FFT_SIZE = 2048
BUFFER_SIZE = 4096
WAVEFORM_POINTS = 512
FLOOR_DB = -90.0
EMPTY_VISUAL = MappingProxyType({'waveform': (), 'spectrum': (), 'frequencies': ()})


def _fft(values):
    """Iterative radix-2 FFT, deliberately independent of optional DSP packages."""
    size = len(values)
    result = [complex(value) for value in values]
    reverse = 0
    for index in range(1, size):
        bit = size >> 1
        while reverse & bit:
            reverse ^= bit
            bit >>= 1
        reverse ^= bit
        if index < reverse:
            result[index], result[reverse] = result[reverse], result[index]
    span = 2
    while span <= size:
        angle = -2 * math.pi / span
        rotation = complex(math.cos(angle), math.sin(angle))
        for offset in range(0, size, span):
            factor = 1 + 0j
            for index in range(offset, offset + span // 2):
                upper = result[index]
                lower = result[index + span // 2] * factor
                result[index] = upper + lower
                result[index + span // 2] = upper - lower
                factor *= rotation
        span *= 2
    return result


class Meter:
    def __init__(self):
        self.process = None
        self.thread = None
        self.source = None
        self.peak = -90.0
        self.rms = -90.0
        self.clipping = False
        self.error = None
        self._stop = threading.Event()
        self._visual_lock = threading.Lock()
        self._visuals_enabled = False
        self._samples = deque(maxlen=BUFFER_SIZE)
        self._generation = 0
        self._visual_epoch = 0
        self._cached_generation = -1
        self._cached_visual = EMPTY_VISUAL

    @property
    def visuals_enabled(self):
        with self._visual_lock:
            return self._visuals_enabled

    @visuals_enabled.setter
    def visuals_enabled(self, enabled):
        with self._visual_lock:
            if self._visuals_enabled != bool(enabled):
                self._visuals_enabled = bool(enabled)
                self._clear_visuals_locked()

    def _clear_visuals_locked(self):
        self._samples.clear()
        self._visual_epoch += 1
        self._generation += 1
        self._cached_generation = -1
        self._cached_visual = EMPTY_VISUAL

    def start(self, source):
        self.stop()
        self.source = source
        self.error = None
        self._stop.clear()
        try:
            self.process = subprocess.Popen([
                'parec', '--raw', '--format=float32le', '--channels=1', '--rate=48000',
                '--latency-msec=50', '--client-name=Shure Control Linux',
                '--stream-name=Medidor temporal', '--device=' + source,
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, 'LC_ALL': 'C.UTF-8'})
        except OSError as exc:
            self.error = str(exc)
            return
        self.thread = threading.Thread(target=self._read, args=(self.process,), daemon=True)
        self.thread.start()

    @staticmethod
    def _decode(raw):
        samples = array.array('f')
        samples.frombytes(raw[:len(raw) // 4 * 4])
        if sys.byteorder != 'little':
            samples.byteswap()
        # A bad sample must not poison the meter, FFT or Cairo coordinates.
        return [value if math.isfinite(value) else 0.0 for value in samples]

    @staticmethod
    def _levels(samples):
        if not samples:
            return -90.0, -90.0, False
        peak = max(abs(x) for x in samples)
        rms = math.sqrt(sum(x * x for x in samples) / len(samples))
        return (20 * math.log10(max(peak, 10 ** -4.5)),
                20 * math.log10(max(rms, 10 ** -4.5)), peak >= 0.99)

    @staticmethod
    def levels(raw):
        return Meter._levels(Meter._decode(raw))

    def _consume(self, raw):
        """Consume the existing parec stream; no second capture is started."""
        samples = self._decode(raw)
        self.peak, self.rms, self.clipping = self._levels(samples)
        with self._visual_lock:
            if self._visuals_enabled and not self._stop.is_set() and samples:
                self._samples.extend(samples)
                self._generation += 1

    @staticmethod
    def _spectrum(samples):
        frame = list(samples[-FFT_SIZE:])
        frame = [0.0] * (FFT_SIZE - len(frame)) + frame
        window = [0.5 - 0.5 * math.cos(2 * math.pi * index / FFT_SIZE)
                  for index in range(FFT_SIZE)]
        transformed = _fft([value * weight for value, weight in zip(frame, window)])
        coherent_gain = sum(window)
        spectrum = []
        for index, value in enumerate(transformed[:FFT_SIZE // 2 + 1]):
            factor = 1 if index in (0, FFT_SIZE // 2) else 2
            amplitude = abs(value) * factor / coherent_gain
            spectrum.append(max(FLOOR_DB, 20 * math.log10(max(amplitude, 10 ** (FLOOR_DB / 20)))))
        return tuple(spectrum)

    @staticmethod
    def _waveform(samples):
        # Keep both extrema in each bucket, in time order, so brief peaks survive
        # reduction to the widget's bounded number of points.
        if len(samples) <= WAVEFORM_POINTS:
            return tuple(max(-1.0, min(1.0, value)) for value in samples)
        output = []
        buckets = WAVEFORM_POINTS // 2
        for index in range(buckets):
            bucket = samples[index * len(samples) // buckets:(index + 1) * len(samples) // buckets]
            low = min(range(len(bucket)), key=bucket.__getitem__)
            high = max(range(len(bucket)), key=bucket.__getitem__)
            output.extend(max(-1.0, min(1.0, bucket[position])) for position in sorted((low, high)))
        return tuple(output)

    def visual_snapshot(self):
        """Immutable waveform/FFT snapshot. Only a visible consumer requests FFT.

        Frequency bins are Hz; magnitudes are one-sided, Hann-windowed dBFS
        (full-scale bin-centred sine = 0 dBFS). Samples are never saved to disk.
        """
        with self._visual_lock:
            if not self._visuals_enabled or not self._samples or self._stop.is_set():
                return EMPTY_VISUAL
            if self._generation == self._cached_generation:
                return self._cached_visual
            samples = tuple(self._samples)
            generation, epoch = self._generation, self._visual_epoch
        snapshot = MappingProxyType({
            'waveform': self._waveform(samples),
            'spectrum': self._spectrum(samples),
            'frequencies': tuple(index * SAMPLE_RATE / FFT_SIZE for index in range(FFT_SIZE // 2 + 1)),
        })
        with self._visual_lock:
            # Do not resurrect audio after a stop or visibility change while
            # analysis was in progress on another thread.
            if epoch != self._visual_epoch or not self._visuals_enabled or self._stop.is_set():
                return EMPTY_VISUAL
            self._cached_visual = snapshot
            self._cached_generation = generation
            return snapshot

    def _read(self, process):
        try:
            while not self._stop.is_set():
                raw = process.stdout.read(1920 * 4)
                if not raw:
                    if not self._stop.is_set():
                        self.error = process.stderr.read(2000).decode(errors='replace') or 'Captura detenida.'
                    break
                self._consume(raw)
        except (OSError, ValueError) as exc:
            if not self._stop.is_set():
                self.error = str(exc)
        finally:
            # EOF and unplug failures must also discard transient visual data.
            with self._visual_lock:
                self._clear_visuals_locked()

    def stop(self):
        self._stop.set()
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
            if self.thread:
                self.thread.join(timeout=2)
            self.process.stdout.close()
            self.process.stderr.close()
        self.process = None
        self.thread = None
        self.source = None
        self.peak = self.rms = -90.0
        self.clipping = False
        with self._visual_lock:
            self._clear_visuals_locked()
