#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Build source-only archives from committed Git trees, never local build/state files."""
import hashlib
import io
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def git(*args, cwd=ROOT):
    return subprocess.check_output(['git', *args], cwd=cwd)


def main():
    # Never package arbitrary working-directory files or a compiled GPL dependency
    # without an audited complete corresponding-source bundle.
    git('diff', '--exit-code')
    git('diff', '--cached', '--exit-code')
    version_text = git('show', 'HEAD:app/__init__.py').decode()
    version = next(line.split('=', 1)[1].strip().strip('"\'')
                   for line in version_text.splitlines() if line.startswith('__version__'))
    entry = git('ls-tree', 'HEAD', 'integrations/shurectl').decode().split()
    if len(entry) != 4 or entry[0] != '160000':
        raise SystemExit('The committed tree must contain the pinned shurectl submodule.')
    upstream = ROOT / entry[3]
    revision = entry[2]
    if git('rev-parse', 'HEAD', cwd=upstream).decode().strip() != revision:
        raise SystemExit('Initialize the exact pinned submodule before packaging.')
    name = f'shure-control-linux-{version}-source'
    destination = ROOT / 'dist'
    destination.mkdir(exist_ok=True)
    archive = destination / (name + '.tar.gz')
    temporary = archive.with_suffix('.tar.gz.tmp')
    try:
        with tarfile.open(temporary, 'w:gz') as result:
            for cwd, ref, prefix in ((ROOT, 'HEAD', name + '/'),
                                     (upstream, revision, name + '/integrations/shurectl/')):
                content = git('archive', '--format=tar', '--prefix=' + prefix, ref, cwd=cwd)
                with tarfile.open(fileobj=io.BytesIO(content)) as source:
                    for member in source:
                        member.uid = member.gid = 0
                        member.uname = member.gname = ''
                        result.addfile(member, source.extractfile(member) if member.isfile() else None)
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (destination / 'SHA256SUMS').write_text(f'{checksum}  {archive.name}\n')
    print(archive)
    print(checksum)


if __name__ == '__main__':
    main()
