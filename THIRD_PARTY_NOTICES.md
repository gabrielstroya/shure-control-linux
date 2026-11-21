# License and dependency review

Reviewed for the first public source publication on 2026-11-21.

## License chosen for this project

**GPL-3.0-only** applies to the original Python application, scripts, tests,
documentation, screenshots and original microphone icon. The complete GPLv3 text
is preserved in [LICENSE](LICENSE), with the project's copyright declaration in
[COPYRIGHT](COPYRIGHT). This matches the version-only grant declared by shurectl
and avoids introducing incompatible terms into the combined distribution.

This is an independent project. The license does not grant rights to Shure's
trademarks, firmware or proprietary applications. None of those proprietary
components is included. The SVG icon is original project artwork, not a Shure
brand asset. Standard interface icons come from the system desktop theme.

## shurectl

- Upstream: https://github.com/Humblemonk/shurectl
- Version: **2.18.1**
- Pinned commit: **09599a1c7715604f4afd2c4894b4bf5b52bd0c21**
- License: **GPL-3.0-only**, as declared in its `Cargo.toml`.
- Source: Git submodule `integrations/shurectl`; clone with `--recurse-submodules`.
- Modifications: **none** to the upstream source. The Python adapter uses its CLI.
- Upstream copyright, credits and license are retained in the submodule.

We build only the production `shurectl` binary, without the `probe` or `qa`
features. Publishing the application does not transfer authorship of upstream
code. The submodule's original repository and history remain accessible.

## Runtime components provided by the operating system

| Component | License family / upstream reference | How this project uses it |
| --- | --- | --- |
| Python | [PSF license](https://docs.python.org/3/license.html) and included notices | Interpreter and standard library |
| PyGObject | [LGPL-2.1-or-later](https://pygobject.gnome.org/) | System Python GI bindings |
| GTK4 / GLib | [LGPL-2.1-or-later](https://docs.gtk.org/gtk4/) | Native widgets, GObject and D-Bus |
| libadwaita | [LGPL-2.1-or-later](https://gnome.pages.gitlab.gnome.org/libadwaita/doc/main/) | Native desktop widgets |
| Pycairo / Cairo | LGPL/MPL dual licensing; see the [Pycairo project](https://pycairo.readthedocs.io/) and [Cairo licensing](https://www.cairographics.org/license/) | System drawing bindings/libraries |
| PipeWire | [MIT/Expat core, LGPL components and bundled third-party notices](https://gitlab.freedesktop.org/pipewire/pipewire) | Existing audio server and AEC module |
| PulseAudio tools | [LGPL-2.1-or-later and component-specific notices](https://www.freedesktop.org/wiki/Software/PulseAudio/) | `pactl` / `parec` processes |
| WebRTC AEC | [BSD-style license and PATENTS/third-party notices](https://webrtc.googlesource.com/src/+/refs/heads/main/LICENSE) | System PipeWire SPA AEC plugin |

These system libraries and tools are not copied into this source publication.
Their distributor's exact package copyright files remain authoritative for the
installed versions. This table is not a replacement for those notices.

## Rust dependency inventory

[DEPENDENCIES.md](docs/DEPENDENCIES.md) records package versions and declared
license expressions resolved from the pinned `Cargo.lock` for the Linux amd64
metadata graph, including build/development dependencies. It contains no local
paths or account information. The review found no missing license declarations.

Most declarations offer MIT or Apache-2.0. Other declarations include BSD, ISC,
Zlib, Unicode data licenses, the LLVM exception, WTFPL and CDLA-Permissive-2.0.
Alternative licenses are alternatives; `AND` clauses retain all their applicable
notice requirements. The inventory describes metadata, not a claim that every
package is linked into the production binary or a substitute for its actual
license files. Package checksums stay in the upstream lockfile.

## Source publication and future binary releases

This initial repository is **source-only**. Local `bin/`, `target/`, `.build/`,
`dist/`, developer diagnostics and private backups are excluded. The public source
archive builder includes tracked project files and the pinned upstream source;
it does not include a compiled controller.

For a future binary release, provide the complete corresponding source and build
instructions for the exact binary, including required non-system dependencies;
preserve copyright notices, license texts and applicable NOTICE/PATENTS files.
A Cargo.lock or a link to a changing upstream branch alone is not the complete
corresponding source for bundled compiled dependencies. Prepare and audit a
versioned vendor-source bundle before distributing such binaries. Do not remove
upstream notices or apply proprietary restrictions to GPL-covered code.

Primary references: [GPLv3, sections 1, 4–6](https://www.gnu.org/licenses/gpl-3.0.html),
[GNU GPL FAQ on corresponding source](https://www.gnu.org/licenses/gpl-faq.html#DistributeExtendedBinary),
and [GNU license compatibility list](https://www.gnu.org/licenses/license-list.html).
