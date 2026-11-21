# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Synthetic PCM tests: no microphone, GTK display, services or device writes."""
import array
import math
import unittest
from unittest.mock import patch

from app.audio.meter import BUFFER_SIZE, FFT_SIZE, SAMPLE_RATE, WAVEFORM_POINTS, Meter


def pcm(samples):
    return array.array('f', samples).tobytes()


class VisualizerTests(unittest.TestCase):
    def setUp(self):
        self.meter = Meter()
        self.meter.visuals_enabled = True
        self.addCleanup(self.meter.stop)

    def test_sine_frequency_mapping_and_hann_normalization(self):
        frequency = 64 * SAMPLE_RATE / FFT_SIZE
        self.meter._consume(pcm(0.5 * math.sin(2 * math.pi * frequency * index / SAMPLE_RATE)
                                for index in range(FFT_SIZE)))
        snapshot = self.meter.visual_snapshot()
        maximum = max(range(len(snapshot['spectrum'])), key=snapshot['spectrum'].__getitem__)
        self.assertEqual(snapshot['frequencies'][maximum], frequency)
        self.assertAlmostEqual(snapshot['spectrum'][maximum], -6.0206, places=3)
        self.assertEqual(snapshot['frequencies'][-1], SAMPLE_RATE / 2)
        self.assertAlmostEqual(self.meter.peak, -6.0206, places=3)
        self.assertAlmostEqual(self.meter.rms, -9.0309, places=3)

    def test_silence_is_finite_floor_with_flat_waveform(self):
        self.meter._consume(pcm([0] * FFT_SIZE))
        snapshot = self.meter.visual_snapshot()
        self.assertTrue(all(value == -90.0 for value in snapshot['spectrum']))
        self.assertTrue(all(value == 0 for value in snapshot['waveform']))
        self.assertFalse(self.meter.clipping)

    def test_clipping_preserved_in_levels_and_bounded_waveform(self):
        samples = [0] * BUFFER_SIZE
        samples[99], samples[100] = 1.2, -1.1
        self.meter._consume(pcm(samples))
        waveform = self.meter.visual_snapshot()['waveform']
        self.assertTrue(self.meter.clipping)
        self.assertGreater(self.meter.peak, 0)
        self.assertEqual(max(waveform), 1)
        self.assertEqual(min(waveform), -1)

    def test_buffer_and_waveform_are_bounded(self):
        self.meter._consume(pcm([0.5] * (BUFFER_SIZE * 4)))
        self.assertEqual(len(self.meter._samples), BUFFER_SIZE)
        self.assertLessEqual(len(self.meter.visual_snapshot()['waveform']), WAVEFORM_POINTS)

    def test_disabled_never_collects_or_computes_fft(self):
        self.meter.visuals_enabled = False
        with patch.object(Meter, '_spectrum', side_effect=AssertionError('Unexpected FFT')):
            self.meter._consume(pcm([0.5] * FFT_SIZE))
            self.assertEqual(self.meter.visual_snapshot()['spectrum'], ())
        self.assertEqual(len(self.meter._samples), 0)
        self.assertAlmostEqual(self.meter.peak, -6.0206, places=3)

    def test_fft_only_on_request_and_cached_without_new_pcm(self):
        with patch.object(Meter, '_spectrum', wraps=Meter._spectrum) as fft:
            self.meter._consume(pcm([0.5] * FFT_SIZE))
            fft.assert_not_called()
            first = self.meter.visual_snapshot()
            self.assertIs(first, self.meter.visual_snapshot())
            fft.assert_called_once()

    def test_immutable_snapshot_and_stop_clears_samples(self):
        self.meter._consume(pcm([0.5] * FFT_SIZE))
        snapshot = self.meter.visual_snapshot()
        with self.assertRaises(TypeError):
            snapshot['waveform'] = ()
        with self.assertRaises(TypeError):
            snapshot['waveform'][0] = 0
        self.meter.stop()
        self.assertFalse(self.meter._samples)
        self.assertEqual(self.meter.visual_snapshot()['waveform'], ())
        self.assertIsNone(self.meter.process)
        self.assertIsNone(self.meter.thread)

    def test_visibility_change_clears_old_capture(self):
        self.meter._consume(pcm([0.5] * FFT_SIZE))
        self.meter.visuals_enabled = False
        self.meter.visuals_enabled = True
        self.assertEqual(self.meter.visual_snapshot()['spectrum'], ())

    def test_stop_during_analysis_cannot_publish_stale_audio(self):
        self.meter._consume(pcm([0.5] * FFT_SIZE))
        original = Meter._spectrum

        def stop_during_fft(samples):
            self.meter.stop()
            return original(samples)

        with patch.object(Meter, '_spectrum', side_effect=stop_during_fft):
            self.assertEqual(self.meter.visual_snapshot()['spectrum'], ())
        self.assertFalse(self.meter._samples)

    def test_constructing_meter_does_not_capture_or_analyze(self):
        meter = Meter()
        self.assertFalse(meter.visuals_enabled)
        self.assertIsNone(meter.process)
        self.assertIsNone(meter.thread)
        self.assertEqual(meter.visual_snapshot()['spectrum'], ())

    def test_invalid_samples_do_not_poison_visuals(self):
        self.meter._consume(pcm([float('nan'), float('inf'), -float('inf'), 0.5]))
        snapshot = self.meter.visual_snapshot()
        self.assertTrue(all(math.isfinite(value) for value in snapshot['spectrum']))
        self.assertEqual(snapshot['waveform'], (0, 0, 0, 0.5))


if __name__ == '__main__':
    unittest.main()
