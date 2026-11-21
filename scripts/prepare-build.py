# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
from pathlib import Path
import subprocess
root=Path('.build/sysroot').resolve()
for p in Path('.build/debs').glob('*.deb'):
    subprocess.run(['dpkg-deb','-x',str(p),str(root)],check=True)
lib=root/'usr/lib/x86_64-linux-gnu'
for name,target in [('libasound.so','libasound.so.2'),('libudev.so','libudev.so.1'),('libpulse.so','libpulse.so.0')]:
    p=lib/name
    p.unlink(missing_ok=True)
    p.symlink_to('/usr/lib/x86_64-linux-gnu/'+target)
for pc in lib.glob('pkgconfig/*.pc'):
    text=pc.read_text().replace('prefix=/usr',f'prefix={root}/usr').replace('libdir=/usr/',f'libdir={root}/usr/').replace('includedir=/usr/',f'includedir={root}/usr/')
    pc.write_text(text)
