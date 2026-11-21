#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Explicit opt-in hardware acceptance test; changes then restores the same MV7."""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.controllers.shure import ShureController
from app.audio.meter import Meter
from app.core import save_json

parser = argparse.ArgumentParser()
parser.add_argument('--allow-device-changes', action='store_true', required=True)
parser.add_argument('--output', default='docs/evidence/hardware-test.json')
args = parser.parse_args()
controller = ShureController()
before = controller.read()
save_json(Path(args.output).with_suffix('.before.private.json'), before)
results = {'model': before['model'], 'firmware': before['firmware_version'], 'tests': []}
try:
    current = controller.mute(True)
    results['tests'].append({'name': 'mute_on', 'passed': current['muted'] is True})
    controller.mute(before['muted'])
    results['tests'].append({'name': 'mute_restore', 'passed': controller.read()['muted'] == before['muted']})
    if before['settings']['mode'] != 'manual':
        controller.apply({'mode': 'manual'})
    gain = controller.read()['settings']['gain_db']
    test_gain = max(0, gain - 1.5) if gain else 1.5
    current = controller.apply({'gain_db': test_gain})
    results['tests'].append({'name': 'gain_internal', 'target': test_gain, 'readback': current['settings']['gain_db'], 'passed': current['settings']['gain_db'] == test_gain})
    controller.apply({'gain_db': gain})
    for field, value in [('eq_preset', 'presence_boost'), ('compressor', 'light'), ('monitor_mix', 10)]:
        old = controller.read()['settings'][field]
        target = value if old != value else {'eq_preset': 'flat', 'compressor': 'off', 'monitor_mix': 8}[field]
        state = controller.apply({field: target})
        results['tests'].append({'name': field, 'passed': state['settings'][field] == target})
        controller.apply({field: old})
    if 'limiter_enabled' in controller.read()['settings']:
        old = controller.read()['settings']['limiter_enabled']
        state = controller.apply({'limiter_enabled': not old})
        results['tests'].append({'name': 'limiter_readback_only', 'passed': state['settings']['limiter_enabled'] != old})
        controller.apply({'limiter_enabled': old})
    state = controller.apply({'mode': 'auto'})
    results['tests'].append({'name': 'auto_mode', 'passed': state['settings']['mode'] == 'auto'})
    for field, value in [('auto_position', 'far'), ('auto_tone', 'bright')]:
        state = controller.apply({field: value})
        results['tests'].append({'name': field, 'passed': state['settings'][field] == value})
    # Restore automatic controls before manual mode so dormant configuration stays intact.
    controller.apply({k: before['settings'][k] for k in ('auto_position', 'auto_tone')})
except Exception as exc:
    results['error'] = str(exc)
    raise
finally:
    try:
        after = controller.restore(before)
        results['restored'] = after['settings'] == before['settings'] and after['muted'] == before['muted']
        if not results['restored']:
            raise RuntimeError('La restauración no coincide con el estado inicial')
    finally:
        save_json(args.output, results)
print(json.dumps(results, indent=2))
