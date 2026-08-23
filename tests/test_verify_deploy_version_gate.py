"""verify-deploy passes when the live version is NEWER than the tag it built.

Three of eleven releases on 2026-08-22 never deployed under their own tag, and
two of those were normal: ArgoCD Image Updater installs the HIGHEST semver, so
pushing two tags close together always leaves the lower one superseded. Its
verify job polled `/version` for its own tag, timed out at 15 minutes, failed
and posted a Discord alert **indistinguishable from a genuinely stalled deploy**
— which is the failure mode that matters, since prod's auto-deploy has silently
stopped twice before and the dashboard is how anyone finds out.

The gate now accepts >= rather than ==. This test EXECUTES the comparison the
workflow uses, under bash, the way `test_local_tracker_rotation.py` executes the
tracker's rotation under Node: the bug this guards against is arithmetic, and a
grep would only prove the function is spelled right.

The case that matters most is 4.15.10 vs 4.15.9 — a lexical or float compare
calls the newer one older, which would pass a stalled deploy silently. There is
no such version yet, which is exactly why it needs a test rather than a report.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml"


def _ver_ge_source() -> str:
    """The ver_ge function, lifted verbatim out of the workflow.

    Extracted rather than re-typed: a copy in the test would keep passing after
    someone edited the real one.
    """
    m = re.search(r"^\s*ver_ge\(\) \{\n(.*?)^\s*\}\n", WORKFLOW.read_text(), re.S | re.M)
    assert m, "the ver_ge helper moved or was renamed in publish.yml"
    body = "\n".join(line.strip() for line in m.group(1).splitlines() if line.strip())
    return "ver_ge() {\n" + body + "\n}\n"


def _ver_ge(live: str, expected: str) -> bool:
    script = _ver_ge_source() + f'ver_ge "{live}" "{expected}"\n'
    return subprocess.run(["bash", "-c", script], capture_output=True).returncode == 0


def test_the_extracted_function_is_real():
    """A guard that lifted an empty string would pass on anything."""
    src = _ver_ge_source()
    assert "sort -V" in src, "the compare is supposed to be version-aware"
    assert len(src.splitlines()) >= 3


@pytest.mark.parametrize(
    "live,expected,accept",
    [
        # The ordinary pass: the tag deployed as itself.
        ("4.16.0", "4.16.0", True),
        # SUPERSEDED — the reason for the change. Both were live inside the
        # higher tag within minutes on 2026-08-22.
        ("4.15.2", "4.15.1", True),
        ("4.16.0", "4.15.3", True),
        ("4.16.0", "4.13.37", True),
        # A STALLED deploy must still fail. This is the check the job exists for.
        ("4.15.2", "4.16.0", False),
        ("4.13.36", "4.14.0", False),
        # Numeric, not lexical: the case with no live example yet.
        ("4.15.10", "4.15.9", True),
        ("4.15.9", "4.15.10", False),
        ("4.9.0", "4.10.0", False),
        ("4.10.0", "4.9.0", True),
        # Major boundaries.
        ("5.0.0", "4.16.0", True),
        ("4.16.0", "5.0.0", False),
    ],
)
def test_the_version_gate(live, expected, accept):
    assert _ver_ge(live, expected) is accept, (
        f"live {live} vs built {expected}: expected {'pass' if accept else 'FAIL'}"
    )


def test_the_superseded_branch_is_GATED_on_the_comparison():
    """The comparison being right is only half of it — it also has to be what
    decides the branch.

    This is a shape check, not an execution one: `check()` wraps curl and jq, so
    driving it would mean stubbing both. Replacing the condition with something
    that always passes is the mutation this catches, and it is the one that
    would turn a stalled deploy into a green tick.
    """
    text = WORKFLOW.read_text()
    m = re.search(r"if (.+?); then\n\s*MODE=\"SUPERSEDED\"", text)
    assert m, "the SUPERSEDED branch is no longer guarded by a condition"
    assert "ver_ge" in m.group(1), f"guarded by {m.group(1)!r}, not by the version compare"


def test_the_success_message_does_not_claim_the_wrong_thing():
    """A superseded run must not announce "X deployed and verified live" — X did
    not deploy; the version containing it did, and the Discord line is the only
    record most people read."""
    text = WORKFLOW.read_text()
    assert 'MODE="SUPERSEDED"' in text
    assert "is live inside" in text, "the superseded path needs its own wording"
    # ...and the failure path must still exist for a genuinely older version.
    assert "did not go live within 15 min" in text
