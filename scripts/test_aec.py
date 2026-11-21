#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Opt-in live AEC lifecycle test, restores original routes even on failure."""
import argparse
import json
from pathlib import Path
import sys
import subprocess
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.audio.pipewire import PipeWire, arguments
from app.core import save_json

parser = argparse.ArgumentParser()
parser.add_argument('--allow-audio-changes', action='store_true', required=True)
parser.add_argument('--output', default='docs/evidence/aec-test.json')
args = parser.parse_args()
output = Path(args.output)
journal = output.with_suffix('.session.private.json')
if journal.exists():
    raise SystemExit('Ya existe una sesión pendiente: restaurarla antes de repetir.')
p = PipeWire(journal=journal)
before = p.snapshot()
originals = p.echo_modules(before)
if len(originals) != 1:
    raise SystemExit('Esta prueba espera exactamente un AEC existente.')
route = arguments(originals[0])
source, sink = route['source_master'], route['sink_master']
save_json(output.with_suffix('.before.private.json'), before)
# Silent playback and a discarded capture exercise live stream migration without
# storing audio or playing a test tone. These are explicitly synthetic clients.
silent = subprocess.Popen(['paplay', '--raw', '--format=s16le', '--channels=2', '--rate=48000',
                           '--client-name=SCL acceptance silence', '--device=' + before['default_sink']],
                          stdin=open('/dev/zero','rb'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
capture = subprocess.Popen(['parec', '--raw', '--client-name=SCL acceptance discard',
                            '--device=' + before['default_source']], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
import time
time.sleep(0.4)
before = p.snapshot()
results = {'tests': [], 'acoustic_quality': 'not_tested', 'synthetic_clients': ['silent playback', 'discarded capture']}
try:
    p.enable(source, sink, adopt=True)
    p.enable(source, sink)
    assert len(p.echo_modules(p.snapshot())) == 1
    results['tests'].append('adopt_and_no_duplicates')
    p.disable()
    assert not p.echo_modules(p.snapshot())
    results['tests'].append('disable_and_restore_physical_defaults')
    p.enable(source, sink)
    assert len(p.echo_modules(p.snapshot())) == 1
    results['tests'].append('create_webrtc_virtual_devices')
    p.disable()
    results['tests'].append('remove_owned_virtual_devices')
finally:
    p.restore()
    after = p.snapshot()
    results['defaults_restored'] = all(before[k] == after[k] for k in ('default_source', 'default_sink'))
    results['modules_restored'] = [m['argument'] for m in originals] == [m['argument'] for m in p.echo_modules(after)]
    results['physical_devices_preserved'] = all(
        [d['name'] for d in p.physical(before, k)] == [d['name'] for d in p.physical(after, k)] for k in ('sources', 'sinks'))
    stream_results = []
    for kind, field, devices in [('sink-inputs','sink','sinks'),('source-outputs','source','sources')]:
        old_names = {d['index']:d['name'] for d in before[devices]}
        new_names = {d['index']:d['name'] for d in after[devices]}
        for old in before[kind]:
            if old.get('client') is None or old.get(field) not in old_names:
                continue
            new = next((s for s in after[kind] if s['index']==old['index'] and s.get('client')==old.get('client')),None)
            if new:
                stream_results.append(old_names[old[field]] == new_names.get(new[field]))
    results['surviving_stream_count'] = len(stream_results)
    results['surviving_stream_routes_restored'] = all(stream_results)
    for process in (silent, capture):
        process.terminate()
        process.wait(timeout=3)
    save_json(output, results)
print(json.dumps(results,indent=2))
assert all(results[k] for k in ('defaults_restored','modules_restored','physical_devices_preserved','surviving_stream_routes_restored'))
