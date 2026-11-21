# Testing and known limitations

Version 0.2.1 is experimental. Passing control and routing tests does **not** establish
acoustic quality, reliable echo reduction in a call, or support for other Shure models.

## Publication checks — 2026-11-21

| Check | Result | Scope |
| --- | --- | --- |
| Application unit suite | 144 passed | Fake USB/audio transports, profiles, routing ownership, AEC recovery, resident state and UI status |
| Pinned shurectl unit suite | 394 passed, 1 ignored | Upstream tests of the production binary; the ignored firmware-archive test requires network access |
| Documentation screenshots | Three actual GTK views rendered | Deterministic demo data, no HID access or audio capture; not hardware test evidence |
| Per-user installation/uninstallation | Passed with an isolated temporary home | Launcher reports 0.2.1, licenses and upstream source included, owned installation removed and preferences retained |
| Dependency inventory | 239 Cargo packages have license declarations | Includes build/development dependencies for Linux amd64; declaration inventory, not a substitute for redistribution review |

The Python suite was run with the Ubuntu system interpreter and GTK libraries:

```sh
GSK_RENDERER=cairo GSETTINGS_BACKEND=memory \
  /usr/bin/python3 -m unittest discover -s tests -v
```

It does not require a microphone or change live audio. GTK tests need a display;
for a headless environment with Xvfb installed, use:

```sh
dbus-run-session -- xvfb-run -a env GSK_RENDERER=cairo GSETTINGS_BACKEND=memory \
  /usr/bin/python3 -m unittest discover -s tests -v
```

After installing the documented development libraries, the upstream tests are:

```sh
cargo test --locked --manifest-path integrations/shurectl/Cargo.toml --bin shurectl
```

The publication run used locally extracted development libraries through the
build helper's `PKG_CONFIG_PATH` and `CARGO_HOME`. It did not enable upstream
`probe` or `qa` features. No microphone firmware was modified.

## Previously completed physical-device checks — 2026-11-20

These results were obtained during development on the original USB MV7
(`14ed:1012`), Ubuntu 26.04.1 amd64, PipeWire 1.6.2 and a Renoir/Cezanne HDMI output.
They were reviewed for this publication; live control writes were not repeated
just to prepare the repository. Raw local reports and device-specific journals
are intentionally excluded from the public source.

| Area | Verified behavior | Limit |
| --- | --- | --- |
| HID permissions | Normal-user access with the exact-model udev rule; other USB devices unchanged | Active local desktop session; physical unplug/replug still pending |
| MV7 controls | Ten hardware checks, including reversible gain, mute, mode, EQ, compressor, limiter, tone, distance and Monitor Mix changes | Readback proves parameter changes, not audible DSP quality; not every setting combination tested |
| GTK controls and profiles | Actual widgets/callbacks, profile application and restoration, live meter | Not an accessibility or usability certification |
| PipeWire volume and streams | Temporary volume change restored; synthetic silent-playback/discarded-capture clients exercised route restoration | Not a real conferencing application's stream lifecycle |
| AEC stale-journal regression | Eleven live checks, including two WebRTC cycles, duplicate prevention and recovery with a new manager | Control-plane state only; no acoustic evaluation |
| Full application exit/restart | Three separate GTK processes: retire an obsolete journal, preserve/recover an owned AEC, restore on exit, then enable/disable a fresh session | Application restart, not an OS reboot |
| AEC test cleanup | Physical defaults, modules, volumes and mute restored; internal MV7 settings unchanged by the AEC regressions | Concurrent changes from other programs can legitimately block guarded restoration |

The stale-journal cause and safeguards are described in [AEC-RECOVERY.md](AEC-RECOVERY.md).
PipeWire was not restarted and global audio configuration was not changed.

## Opt-in live tests

Close Shure Control Linux completely and inspect each script before running it.
Use the normal desktop user, not root. Keep other applications from changing
devices during the test. Reports can contain private environment information;
they belong in ignored local paths, not issues or commits.

- `test_hardware.py`, `test_ui.py` and `test_resident.py` require
  `--allow-device-changes`. They can change real microphone controls and, for UI
  and resident scenarios, audio routes. They attempt guarded restoration.
- `test_aec_reconciliation_live.py` requires `--allow-audio-changes`. It starts
  with zero AEC modules and the selected physical devices as defaults. Pass
  `--source` and `--sink` using your actual `pactl` names; its defaults describe
  the original development MV7/HDMI pair.
- `test_aec_restart_live.py --allow-audio-changes` requires a physical Shure input
  and HDMI output as defaults, with zero AEC modules. It opens three GTK processes
  and reads HID state without writing microphone controls.
- `test_aec.py --allow-audio-changes` is a different, adoption test. It expects
  exactly one existing AEC, temporarily manages it, creates silent playback and
  discarded capture clients, and attempts to restore it. Do not use this as the
  fresh-session test.
- `test_singleton.py --allow-desktop-test` checks a real desktop D-Bus owner and
  tray host with temporary settings. It needs the MV7 and a compatible tray host.

These are development acceptance scripts, not unattended diagnostics. If cleanup
reports a conflict, review the retained private recovery state instead of forcing
routes or unloading unrelated modules.

Reproduce documentation images without hardware:

```sh
GSK_RENDERER=cairo GSETTINGS_BACKEND=memory \
  /usr/bin/python3 scripts/capture_screenshots.py
```

Before publishing source, stage only reviewed files and run
`/usr/bin/python3 scripts/check-publication.py`. This catches common private
artifacts and credential patterns; manual review is still required.
`scripts/build-dist.py` creates a source-only archive from a clean committed tree,
including the exact pinned upstream source. It never packages local executables.

## Pending acceptance and limitations

- **Acoustic AEC validation in a real call is pending.** With another person's
  agreement, compare AEC off/on using the same speaker volume and microphone
  placement, then test simultaneous speech, residual echo and intelligibility.
  Select both virtual devices in the call application. No voice recording is
  required or included with this project.
- Physical USB disconnect/reconnect and a complete desktop/OS restart remain
  unverified. Do not infer these from simulated disconnection or process restart.
- Only the original MV7 on the stated Ubuntu amd64 environment has been tested.
  Other microphones, distributions and architectures are unsupported/unverified.
- Global shortcuts depend on the desktop portal. Some hosts require permission
  again after restarting; tray availability depends on the desktop extension/host.
- PipeWire commands are separate operations, not an atomic transaction against
  other clients. Ownership checks refuse detected conflicts; review diagnostics
  when another program changes routes concurrently.
- AEC is a session module, not persistent global PipeWire configuration. Keeping
  AEC on application exit does not promise survival across an audio-service or OS
  restart. Existing application streams may need explicit rerouting.
- The UI is Spanish. Translation, keyboard/accessibility review, acoustic DSP
  measurements and independent headphone-volume control are not complete.
