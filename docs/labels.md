# Issue labels — automation semantics

Most labels are for triage and don't affect automation. Two do, because the
post-ship release automation (`.github/workflows/publish.yml`, the `post-ship`
job) reads them. Their meaning is load-bearing — keep it exact.

## `ac-verified`

**Meaning:** a human has confirmed this issue's **acceptance criteria match what
actually shipped** — the criteria describe the real change, not a mis-diagnosed
mechanism.

**When to apply:** ONLY after the fix is verified. Never at triage, never at
issue creation. Applying it is an assertion that the issue's record is correct.

**What it does:** on a release whose commits reference this issue, the post-ship
job **auto-closes** it with a comment quoting the shipped version and the release
diff stat. Auto-close depends entirely on this label meaning exactly what it says.

**Why the gate exists:** an issue can be "fixed" while its acceptance criteria
describe the wrong thing. Example: #53's ACs described a finish-radio search
trigger that was never the bug (the real defect was a stale-response race).
Closing such an issue against its stale criteria records a phantom fix (cf. the
#66 problem). So: no `ac-verified` label → the release automation **flags the
issue for review instead of closing it**. Do not apply this label to shortcut a
close; apply it only when the criteria are genuinely correct.

## `needs-ac-review`

**Applied by:** the post-ship job, automatically, when a released-against issue
is **not** `ac-verified`. It marks "shipped code references this issue, but its
acceptance criteria haven't been confirmed — a human must reconcile them before
closing."

**When to remove:** after reviewing the criteria against what shipped. Then
either correct the criteria and apply `ac-verified` (and close), or close
manually with an explanation. The automation adds this label idempotently and
leaves the issue open.
