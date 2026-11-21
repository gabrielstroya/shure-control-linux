# MV7 USB HID permissions

The supported microphone is the **original Shure MV7**, VID:PID **14ed:1012**.
Audio capture through PipeWire and configuration through HID are separate paths.
Use normal desktop-session permissions; do not run the GUI as root.

The project-specific rule in [`packaging/62-shure-mv7.rules`](../packaging/62-shure-mv7.rules) is:

```udev
ACTION!="remove", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="14ed", ATTRS{idProduct}=="1012", TAG+="uaccess"
```

This matches only HID raw nodes whose USB ancestor has that exact vendor/product
pair. It grants access through the active local session's `uaccess` ACL mechanism.
It does not use global `0666` permissions, change audio nodes, match other USB
products, or create a broad device-access group. Multiple original MV7 units
with the same USB identifiers will all match; the rule is model-specific, not
serial-specific. Remote/headless sessions may not receive a local-seat ACL.

## Install explicitly

Review the file first. If a rule with the same destination name already exists,
compare it and preserve your previous version before replacing it.

```sh
sudo install -m 0644 packaging/62-shure-mv7.rules /etc/udev/rules.d/62-shure-mv7.rules
sudo udevadm control --reload-rules
```

Reconnect the microphone when it will not interrupt a call or recording. To
apply without unplugging, target only the matching HID nodes:

```sh
for node in /sys/class/hidraw/hidraw*; do
  [ -r "$node/device/uevent" ] || continue
  if grep -qx 'HID_ID=0003:000014ED:00001012' "$node/device/uevent"; then
    sudo udevadm trigger --action=change "$node"
  fi
done
```

Check access from the normal user session:

```sh
for node in /sys/class/hidraw/hidraw*; do
  [ -r "$node/device/uevent" ] || continue
  if grep -qx 'HID_ID=0003:000014ED:00001012' "$node/device/uevent"; then
    device="/dev/${node##*/}"
    ls -l "$device"
    test -r "$device" && test -w "$device" && printf '%s: readable and writable\n' "$device"
  fi
done
```

Do not install the broader upstream shurectl rule in place of this rule when
only MV7 access is intended.

## Revert

Remove **only this project-specific rule**, or restore the rule you backed up if
you replaced one:

```sh
sudo rm /etc/udev/rules.d/62-shure-mv7.rules
sudo udevadm control --reload-rules
```

Reconnect the MV7 to recreate its device nodes and remove the old session ACL.
Removing a file and reloading rules alone may leave an ACL on an already-existing
node until it is recreated. No PipeWire restart is needed. The application
uninstaller deliberately leaves this administrative rule untouched.
