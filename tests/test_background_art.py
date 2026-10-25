"""Execute image failures, partner isolation, and pre-live deck-change races."""

import pathlib
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_background_image_recovery():
    source = (
        pathlib.Path(__file__).resolve().parents[1] / "app/static/background-art.js"
    ).read_text()
    script = (
        """
const assert = require('node:assert/strict');
const images = [];
const timers = new Map();
let timerId = 0;
function setTimeout(callback, delay) {
  assert.equal(delay, 5000);
  timers.set(++timerId, callback);
  return timerId;
}
function clearTimeout(id) { timers.delete(id); }
global.Image = class { constructor() { images.push(this); } };
const document = {addEventListener() {}};
const values = {};
const el = {style: {
  removeProperty(p) {delete values[p];},
  setProperty(p, v) {values[p] = v;}
}};
"""
        + source
        + """
setBackgroundArt(el, 'primary', ['crop', 'mirror', 'redirect']);
assert.equal(images[0].src, 'crop');
images[0].onerror();
assert.equal(images[1].src, 'mirror');
images[1].onload();
assert.equal(values.primary, 'url("mirror")');
setBackgroundArt(el, 'primary', ['crop', 'mirror', 'redirect']);
assert.equal(images.length, 2); // a poll must retain the recovered image
setBackgroundArt(el, 'secondary', ['partner', 'partner-fallback']);
images[2].onerror();
images[3].onload();
assert.equal(values.secondary, 'url("partner-fallback")');
assert.equal(values.primary, 'url("mirror")');
setBackgroundArt(el, 'primary', ['old-deck']);
const stale = images.at(-1);
setBackgroundArt(el, 'primary', ['new-deck']);
images.at(-1).onload();
stale.onload();
assert.equal(values.primary, 'url("new-deck")');
setBackgroundArt(el, 'primary', []);
stale.onload();
assert.equal(values.primary, undefined);
setBackgroundArt(el, 'primary', ['fail1', 'fail2', 'fail3']);
images.at(-1).onerror();
images.at(-1).onerror();
const count = images.length;
images.at(-1).onerror();
assert.equal(images.length, count); // exhaustion must not loop
assert.equal(values.primary, undefined);
assert.equal(timers.size, 0);
setBackgroundArt(el, 'primary', ['stalled', 'working-mirror']);
const stalled = images.at(-1);
assert.equal(stalled.src, 'stalled');
assert.equal(timers.size, 1);
[...timers.values()][0](); // no load OR error from the first host
assert.equal(images.at(-1).src, 'working-mirror');
images.at(-1).onload();
assert.equal(values.primary, 'url("working-mirror")');
stalled.onload(); // a late response must not overwrite the fallback
stalled.onerror();
assert.equal(values.primary, 'url("working-mirror")');
assert.equal(timers.size, 0);
"""
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
