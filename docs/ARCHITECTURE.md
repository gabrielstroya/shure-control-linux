# Architecture and privacy

The application runs on the Ubuntu computer physically connected to the MV7.
It does not connect to a Raspberry Pi, speech service, AI assistant, legal platform
or account provider. Legacy/service profile names are labels for local audio
settings, not integrations.

- `app/ui`: GTK4/libadwaita window and single-instance `Gio.Application` lifecycle.
- `app/controllers/runtime.py`: one serialized worker shared by the window, tray,
  shortcuts and CLI. Periodic device reads do not start capture or apply profiles.
- `app/controllers/shure.py`: validated CLI/JSON adapter for unmodified shurectl,
  restricted to the original MV7. Preset writes use a temporary isolated XDG
  directory and verify readback; normal upstream user presets are preserved.
- `app/audio/pipewire.py`: live PulseAudio-compatible PipeWire inventory, explicit
  module adoption, guarded changes and private reversible journal.
- `app/audio/meter.py`: on-demand `parec`, bounded in-memory samples, peak/RMS and
  FFT. This measures the PipeWire stream, not a USB hardware meter.
- `app/desktop`: StatusNotifierItem/dbusmenu, optional XDG autostart and portal
  shortcuts. No installed system daemon or extension.
- `app/profiles`: private validated JSON settings; no credentials or mute values.
- `app/diagnostics`: local reports. Serial numbers are omitted from public-facing
  diagnostic/status output, but device names and process information still need
  review before sharing.

The production application uses no web API. It calls the shurectl CLI, which exits
before its upstream interactive TUI/firmware-archive feature. The upstream source
contains those other features; this application does not invoke them. Builds may
access package registries. No firmware-update functionality is exposed.

Writes require an explicit action and subsequent verification. A disconnect or
partial device response does not count as success. There is no atomic transaction
across HID or independent PipeWire clients; external changes can race commands.
The application re-reads identities/defaults and stops on ambiguity rather than
claiming universal protection against all concurrent changes.
