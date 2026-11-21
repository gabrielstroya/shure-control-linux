# Installation

Use the [README quick start](../README.md#build-and-install) for a compatible
Ubuntu system. The required libadwaita API is 1.5 or newer (`Adw.AlertDialog`);
GTK 4.10 or newer and Python 3.11 or newer are required. Only Ubuntu 26.04.1
amd64 has been tested with the physical device. There is no Flatpak, Snap or
PyPI package in this publication.

## Pinned dependency

```sh
git submodule update --init --recursive
git -C integrations/shurectl rev-parse HEAD
```

Expected revision: `09599a1c7715604f4afd2c4894b4bf5b52bd0c21` (shurectl 2.18.1).
Do not replace this with the current upstream branch without compatibility tests.

The normal build uses system development libraries:

```sh
cargo build --locked --release --bin shurectl --manifest-path integrations/shurectl/Cargo.toml
```

An optional Ubuntu **amd64-only** helper can extract development `.deb` files
into `.build/sysroot` without installing those development packages globally:

```sh
bash scripts/build-controller.sh
```

It needs `apt-get download`, `dpkg-deb`, pkg-config, a compiler and an existing
Rust/Cargo 1.89+ toolchain. It uses the machine's configured package repositories;
repository authentication or unavailable packages can make it fail. It does not
replace the runtime GTK/PipeWire dependencies, does not run sudo and does not
change services. `.build` is private local build output and is never published.

## Per-user installation

Run `/usr/bin/python3 scripts/install.py` without sudo. The installer creates:

- `~/.local/share/shure-control-linux/releases/<release-id>` and a `current` link.
- `~/.local/bin/shure-control-linux`.
- A desktop entry and the original application/status icons under `~/.local/share`.
- A Python venv with `--system-site-packages --without-pip` using Ubuntu's GI.

It preserves earlier releases and user preferences. Existing foreign launchers
or modified icons are not silently replaced. HID permissions are configured
separately; see [USB-ACCESS.md](USB-ACCESS.md).

The source checkout can also run directly with `/usr/bin/python3 -m app.main`.
The application looks for `bin/shurectl` first, then the local upstream release
build; `SHURECTL_BIN` is a development override.

## Audio prerequisites

Check the existing user audio session:

```sh
pactl info
pactl get-default-source
pactl get-default-sink
pactl list short sources
pactl list short sinks
```

The server must be PipeWire with PulseAudio compatibility and its WebRTC AEC SPA
plugin. The application uses `module-echo-cancel` with `aec_method=webrtc`, as
described by [PipeWire](https://docs.pipewire.org/page_pulse_module_echo_cancel.html).
Installing this application does not replace an existing audio stack, restart
PipeWire/WirePlumber or install global PipeWire configuration.

## Troubleshooting

- **No HID access:** inspect the device-specific rule and active desktop session;
  never start the entire GUI with sudo.
- **Missing GI namespace or widget:** verify the minimum GTK/libadwaita versions
  and use Ubuntu's `/usr/bin/python3`, not a Python environment without GI.
- **No tray host:** the window remains accessible. No desktop extension is installed.
- **No shortcut portal:** use normal desktop shortcuts or the CLI; support depends
  on the compositor/portal implementation.
- **Changed audio routes:** inspect the live devices and managed session. Do not
  delete or overwrite another program's modules to force an AEC transition.
