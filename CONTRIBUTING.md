# Contributing

Shure Control Linux is an independent, unofficial project for the original MV7
on Ubuntu. Keep changes focused and distinguish observed hardware behavior from
simulated tests or acoustic assumptions.

1. Clone with `--recurse-submodules` and follow the README build instructions.
2. Create a branch for your change; keep the pinned shurectl revision unless the
   change explicitly updates and tests the dependency.
3. Add meaningful regression coverage for device/routing behavior.
4. Run `GSK_RENDERER=cairo /usr/bin/python3 -m unittest discover -s tests -v`.
5. Explain the problem, resulting behavior and validation in your pull request.

Do not commit local logs, profiles, state journals, credentials, serial numbers,
recordings, build outputs or another application's files. Use synthetic data in
fixtures and review screenshots before publishing. Device tests require an
explicit opt-in and must preserve settings. Never run them during a live call.

Do not add firmware writes, speculative HID commands, global audio resets or
silent adoption/removal of another application's modules. Report failures
honestly and preserve recoverable state. The current UI is Spanish; English
translation work should preserve behavior and be tested separately.

Contributions to original project files are submitted under GPL-3.0-only, with
appropriate authorship retained. Do not import incompatible or unlicensed code.
Changes to shurectl belong in its upstream project; read its contribution and
licensing instructions before proposing a dependency change.
