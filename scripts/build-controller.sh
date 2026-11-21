#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# No global dependencies are installed. Use dev .deb files extracted locally.
mkdir -p .build/debs .build/sysroot
(cd .build/debs && apt-get download libasound2-dev libudev-dev libpulse-dev libpulse-mainloop-glib0)
python3 scripts/prepare-build.py
export PKG_CONFIG_PATH="$PWD/.build/sysroot/usr/lib/x86_64-linux-gnu/pkgconfig"
export CARGO_HOME="$PWD/.build/cargo"
cargo build --locked --release --bin shurectl --manifest-path integrations/shurectl/Cargo.toml
