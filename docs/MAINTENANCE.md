# Usage, maintenance and reversal

## Closing and resident mode

Closing a window can ask what to do, exit, hide it, or retain the controller.
**Salir completamente** / `--quit` exits the controller, indicator and shortcuts.
An audio restoration conflict can prevent full exit so the current routes remain
available for review. Never force a conflicting restore solely to dismiss a warning.

**Conservar AEC al salir** is initially enabled. PipeWire can keep AEC after the
GUI exits. Disable this preference to request conservative audio restoration on
full exit. Login autostart and resident mode are initially disabled; installation
preserves an explicit user choice.

The meter starts only on request. Hiding/closing the window, leaving the microphone
page or a supported minimization notification stops capture. Reopening does not
restart it. No audio is saved to disk by the application.

## Data locations

Settings respect `XDG_CONFIG_HOME` (normally `~/.config`); state respects
`XDG_STATE_HOME` (normally `~/.local/state`), under `shure-control-linux`:

- `desktop.json`: resident/close/autostart/shortcut preferences.
- `preferences.json`: selected audio devices.
- `profiles.json`: local custom settings.
- `hardware-before.json`: initial microphone backup, including device identity.
- `audio-session.json`: reversible audio-session journal.
- `history/*.private.json`: archived obsolete audio journals.
- `events.log`: bounded rotating event log.

JSON files are written atomically with private permissions. Device names, process
information and identities can still be personal data: inspect before sharing.
Do not commit these directories or diagnostic exports to the public repository.

## Update and rollback

Exit fully, run `git pull --ff-only`, update the pinned submodule, rebuild the
controller and rerun `scripts/install.py`, as shown in the README. The installer
creates a new release and retains previous ones. It does not overwrite profiles
or enable autostart.

To roll back, exit the application, review any pending audio session and select
an existing previous directory under `~/.local/share/shure-control-linux/releases`.
Replace the `current` symlink atomically with a link to that chosen directory.
Do not blindly restore an old audio journal: its devices or module ownership may
no longer exist. A manual symlink change does not rewrite `.installation.json`;
inspect `current` to identify the effective installation.

## Audio and microphone restoration

**Deshacer** restores the previous microphone state held in the session.
**Restaurar micrófono** uses its initial local backup and requires a matching
microphone identity. These are explicit hardware writes.

**Desactivar AEC** removes the managed AEC and restores available physical routes.
**Restaurar audio** can also recreate an explicitly adopted earlier AEC session.
External default/volume changes and ambiguous module ownership block automatic
restoration. Stream moves are undone only when client identity and expected
current destination still match.

Since 0.2.1, a vanished AEC session with obsolete route claims can be reconciled:
the old journal is archived, physical defaults must be present, known virtual
nodes absent, and consecutive inventories stable. Reconciliation writes only
local metadata. A deliberately disabled session with matching routes retains
its valid undo history. See [AEC recovery](AEC-RECOVERY.md).

## Autostart and shortcuts

Use **Preferencias** for the resident controller and optional login startup.
Autostart creates an owned XDG desktop entry, not a system service. It starts
`--background`, never a profile, capture or AEC session automatically.

```sh
/usr/bin/python3 scripts/autostart.py --status
/usr/bin/python3 scripts/autostart.py --enable --background
/usr/bin/python3 scripts/autostart.py --disable
```

Global shortcuts require the desktop portal and explicit interactive assignment.
No shortcuts are reserved by default. Some desktops require configuring the
portal session again after a full exit. Alternatively, bind the full path to
`~/.local/bin/shure-control-linux --toggle-mute`, `--show` or `--next-profile` using
your desktop's own shortcut settings; expand `~` if the settings field does not.

## Uninstall

Restore audio (and microphone settings if wanted) using the application, exit
fully, then run `/usr/bin/python3 scripts/uninstall.py` from a source checkout.
The uninstaller removes owned application entries/releases, preserving modified
or foreign desktop files. It keeps settings, backups and the udev rule.

Only if no recovery data is needed, remove the `shure-control-linux` directories
under your actual XDG config/state locations manually. Review their contents
first. The optional [udev reversal](USB-ACCESS.md#revert) is a separate administrative
action. Keep your source checkout if you need to build/install again.
