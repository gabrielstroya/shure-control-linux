#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Check the staged public tree for forbidden artifacts and recognizable secrets.

This conservative check supplements manual review, not a guarantee of secrecy.
The upstream submodule is checked by its pinned revision, not recursively staged.
"""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BLOCKED_PARTS = {'.build', '.venv', '__pycache__', '.codex', '.agents', '.aws',
                 'target', 'bin', 'dist', 'local.private.data', 'evidence'}
BLOCKED_SUFFIXES = {'.pyc', '.log', '.wav', '.flac', '.mp3', '.ogg', '.m4a', '.pcm',
                    '.raw', '.db', '.sqlite', '.pem', '.key', '.p12', '.pfx'}
PATTERNS = {
    'private key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'GitHub credential': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})'),
    'cloud credential': re.compile(rb'(?:AKIA|ASIA)[A-Z0-9]{16}'),
    'personal absolute path': re.compile(rb'/(?:home|Users)/[A-Za-z0-9_.-]+/'),
    'personal email': re.compile(rb'[A-Za-z0-9_.+-]+@(?:gmail|hotmail|outlook|yahoo)\.[a-z]+'),
}


def main():
    entries = subprocess.check_output(['git', 'ls-files', '--stage', '-z'], cwd=ROOT).split(b'\0')
    issues, count = [], 0
    for entry in filter(None, entries):
        metadata, filename = entry.split(b'\t', 1)
        mode, sha, stage = metadata.split()
        path = Path(filename.decode())
        count += 1
        if stage != b'0':
            issues.append(f'{path}: unresolved merge')
        if mode == b'160000':
            if str(path) != 'integrations/shurectl' or sha != b'09599a1c7715604f4afd2c4894b4bf5b52bd0c21':
                issues.append(f'{path}: unreviewed dependency revision')
            continue
        if (set(path.parts) & BLOCKED_PARTS or path.suffix.lower() in BLOCKED_SUFFIXES or
                any('.private.' in part or part.startswith('.env') for part in path.parts)):
            issues.append(f'{path}: local/private artifact')
        content = subprocess.check_output(['git', 'cat-file', 'blob', sha.decode()], cwd=ROOT)
        if content.startswith(b'\x7fELF'):
            issues.append(f'{path}: compiled executable')
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                issues.append(f'{path}: {label}')
    if not count:
        issues.append('No staged public files')
    if issues:
        raise SystemExit('\n'.join(issues))
    print(f'Public index checked: {count} entries; no blocked artifacts or recognizable secrets.')


if __name__ == '__main__':
    main()
