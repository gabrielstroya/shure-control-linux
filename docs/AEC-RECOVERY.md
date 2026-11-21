# AEC journal reconciliation in 0.2.1

An earlier implementation reloaded `audio-session.json` and compared its
`expected_defaults` with the current defaults even when its AEC session no longer
existed. If the journal expected old virtual devices, closing and restarting the
application reproduced the same warning indefinitely.

`PipeWire.reconcile()` now checks the live inventory before route operations and
on state refresh. For an obsolete session, it requires no live AEC modules, no
known virtual nodes, valid physical defaults and stable repeated inventories.
It archives the original bytes with mode 0600 and establishes the current routes
as the new baseline. It performs **no audio writes** during reconciliation.

Independent volume undo entries and still-valid stream moves retain their guards.
An intentionally disabled session whose physical defaults still match keeps its
original restore history. A stale session does not recreate its historical AEC.
Active conflicts, foreign modules and orphaned virtual nodes remain protected.
Module identity is checked again before unloading to guard against index reuse.

The UI clears the resolved defaults warning, toast and desktop notification after
successful reconciliation. Audio-operation errors are separate from USB connection
errors. Unrelated warnings are not suppressed.

Regression tests include archival failure, concurrent defaults/modules/journal
changes, externally changed volumes, orphaned virtual nodes, repeated enable/disable,
module index reuse and process restart. [Testing](TESTING.md) separates unit tests,
physical-device lifecycle checks and the still-pending acoustic call evaluation.
