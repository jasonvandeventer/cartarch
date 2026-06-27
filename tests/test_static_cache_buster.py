"""Content-hash static-asset cache buster (issue #29).

`static_v()` keys the `?v=` query string on a static file's CONTENT hash, not
its mtime — so a backend-only deploy (new container = new mtime, identical
content) keeps the browser/CDN cache, while an actual edit busts it. Pins the
issue's acceptance criteria: hash not timestamp, same content → same hash,
changed content → different hash, missing file → version fallback. The static
dir is resolved module-relative (NOT cwd-relative), so these patch ``_STATIC_DIR``.
"""

from __future__ import annotations

import app.dependencies as deps


def _use_static_dir(tmp_path, monkeypatch):
    """Point static_v at a throwaway dir, module-relative (NOT cwd-relative)."""
    static_dir = tmp_path / "static"
    static_dir.mkdir(parents=True)
    monkeypatch.setattr(deps, "_STATIC_DIR", str(static_dir))
    return static_dir


def test_value_is_content_hash_not_timestamp(tmp_path, monkeypatch):
    static_dir = _use_static_dir(tmp_path, monkeypatch)
    (static_dir / "x.css").write_bytes(b"body{}")
    deps._static_hash_cache.clear()

    v = deps.static_v("x.css")
    # A truncated sha256 hex digest — not a 10-digit unix mtime.
    assert len(v) == 12 and all(c in "0123456789abcdef" for c in v)


def test_same_content_same_hash_changed_content_differs(tmp_path, monkeypatch):
    static_dir = _use_static_dir(tmp_path, monkeypatch)
    target = static_dir / "style.css"

    target.write_bytes(b"a{color:red}")
    deps._static_hash_cache.clear()
    h1 = deps.static_v("style.css")

    # Identical content (fresh deploy, new mtime) → identical hash.
    target.write_bytes(b"a{color:red}")
    deps._static_hash_cache.clear()
    h2 = deps.static_v("style.css")
    assert h1 == h2

    # Real edit → different hash.
    target.write_bytes(b"a{color:blue}")
    deps._static_hash_cache.clear()
    h3 = deps.static_v("style.css")
    assert h3 != h1


def test_dev_recomputes_live_prod_caches(tmp_path, monkeypatch):
    static_dir = _use_static_dir(tmp_path, monkeypatch)
    target = static_dir / "style.css"

    # Dev (no APP_VERSION): an edit is picked up live, no restart needed.
    monkeypatch.delenv("APP_VERSION", raising=False)
    deps._static_hash_cache.clear()
    target.write_bytes(b"a{}")
    dev1 = deps.static_v("style.css")
    target.write_bytes(b"b{}")
    dev2 = deps.static_v("style.css")
    assert dev1 != dev2
    assert not deps._static_hash_cache  # dev never populates the cache

    # Prod (APP_VERSION set): hash is frozen per-process even if the file changes.
    monkeypatch.setenv("APP_VERSION", "v1.0.0")
    deps._static_hash_cache.clear()
    target.write_bytes(b"c{}")
    prod1 = deps.static_v("style.css")
    target.write_bytes(b"d{}")
    prod2 = deps.static_v("style.css")
    assert prod1 == prod2


def test_missing_file_falls_back_to_version(tmp_path, monkeypatch):
    _use_static_dir(tmp_path, monkeypatch)
    monkeypatch.setenv("APP_VERSION", "v9.9.9")
    deps._static_hash_cache.clear()
    assert deps.static_v("nope.css") == "v9.9.9"
