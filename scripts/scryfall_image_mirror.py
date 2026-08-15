#!/usr/bin/env python3
"""
scryfall_image_mirror.py — Eager mirror of Scryfall card images.

Downloads display images (small, normal, large, png) and art_crop corpus
for every printing in Scryfall's default_cards bulk dataset.

Prefer this repo's interpreter, whose only third-party need here (`requests`)
is version-pinned in requirements.txt. A system python that happens to have
`requests` also works — but "happens to have" is not a thing to schedule
against:

    .venv/bin/python scripts/scryfall_image_mirror.py \
        --root /mnt/platform/scryfall-images \
        --art-root /mnt/platform/scryfall-artcrop \
        --user-agent "CartarchImageMirror/1.0 (+https://cartarch.com)"

Features:
    - Streaming gzipped JSON Lines — never loads the multi-GB bulk file into RAM
    - Skip-if-present with non-empty check — resumable, idempotent
    - Atomic temp-then-rename writes — no truncated files on interrupt
    - Bounded thread pool with per-download delay (good citizen)
    - Retry with exponential backoff on 429/5xx
    - DFC/MDFC support — front and back faces stored separately
    - Art crop deduplication by illustration_id

Platform issue: jasonvandeventer/vanfreckle-platform#29
Design: cartarch/scryfall-image-mirror-design-2026-06-25.md
"""

import argparse
import gzip
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover - depends on the interpreter, not the code
    # Point at the interpreter that is guaranteed to have it rather than at
    # `pip install`, which invites installing into whichever python happened
    # to run this.
    sys.exit(
        "requests is missing — use the repo venv, where it is pinned:\n"
        f"  {Path(__file__).resolve().parents[1] / '.venv/bin/python'} {Path(__file__).name} ..."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mirror")

SCRYFALL_BULK_API = "https://api.scryfall.com/bulk-data"
DISPLAY_SIZES = ("small", "normal", "large", "png")
RETRY_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
BACKOFF_BASE = 2.0


def shard(uuid_str: str) -> str:
    """First 2 hex chars of a UUID, used as a directory shard."""
    return uuid_str[:2]


def size_ext(size: str) -> str:
    """File extension for a given size."""
    return "png" if size == "png" else "jpg"


def download_file(url: str, dest: Path, session: requests.Session, delay: float) -> bool:
    """
    Download url to dest atomically. Returns True if downloaded, False if skipped.
    Retries on transient errors with exponential backoff.
    """
    if dest.exists() and dest.stat().st_size > 0:
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(MAX_RETRIES):
        try:
            if delay > 0:
                time.sleep(delay)

            resp = session.get(url, stream=True, timeout=30)

            if resp.status_code in RETRY_CODES:
                wait = BACKOFF_BASE**attempt
                log.warning("HTTP %d on %s, retry in %.1fs", resp.status_code, url, wait)
                time.sleep(wait)
                continue

            resp.raise_for_status()

            # Atomic write: temp file then rename
            fd, tmp_path = tempfile.mkstemp(suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        f.write(chunk)
                shutil.move(tmp_path, str(dest))
                return True
            except Exception:
                # Clean up partial temp file
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

        except requests.exceptions.ConnectionError as e:
            wait = BACKOFF_BASE**attempt
            log.warning("Connection error on %s: %s, retry in %.1fs", url, e, wait)
            time.sleep(wait)
            continue
        except requests.exceptions.Timeout:
            wait = BACKOFF_BASE**attempt
            log.warning("Timeout on %s, retry in %.1fs", url, wait)
            time.sleep(wait)
            continue

    log.error("Failed after %d retries: %s", MAX_RETRIES, url)
    return False


def extract_download_tasks(card: dict, root: Path, art_root: Path, seen_illustrations: set):
    """
    Given a card dict from Scryfall bulk data, yield (url, dest_path) tuples
    for all images to download.
    """
    scryfall_id = card.get("id", "")
    if not scryfall_id:
        return

    # Skip cards with no images (tokens without art, etc.)
    has_images = card.get("image_uris") or card.get("card_faces")
    if not has_images:
        return

    s = shard(scryfall_id)

    # Determine faces
    if card.get("card_faces") and any(f.get("image_uris") for f in card["card_faces"]):
        # Multi-face card (DFC, MDFC, split with per-face art)
        faces = []
        for i, face in enumerate(card["card_faces"]):
            if face.get("image_uris"):
                face_name = "front" if i == 0 else "back"
                faces.append((face_name, face["image_uris"]))
    elif card.get("image_uris"):
        # Single-face card
        faces = [("front", card["image_uris"])]
    else:
        return

    # Display images
    for face_name, image_uris in faces:
        for size in DISPLAY_SIZES:
            url = image_uris.get(size)
            if not url:
                continue
            ext = size_ext(size)
            dest = root / s / scryfall_id / face_name / f"{size}.{ext}"
            yield (url, dest)

    # Art crop (deduped by illustration_id)
    illustration_id = card.get("illustration_id")
    if illustration_id and art_root and illustration_id not in seen_illustrations:
        seen_illustrations.add(illustration_id)
        # Art crop URL: prefer from top-level image_uris, fall back to first face
        art_url = None
        if card.get("image_uris") and card["image_uris"].get("art_crop"):
            art_url = card["image_uris"]["art_crop"]
        elif card.get("card_faces"):
            for face in card["card_faces"]:
                if face.get("image_uris") and face["image_uris"].get("art_crop"):
                    art_url = face["image_uris"]["art_crop"]
                    break

        if art_url:
            art_s = shard(illustration_id)
            art_dest = art_root / art_s / f"{illustration_id}.jpg"
            yield (art_url, art_dest)


def get_bulk_download_url(session: requests.Session) -> str:
    """Fetch the default_cards bulk data download URL from Scryfall."""
    log.info("Fetching bulk data listing from %s", SCRYFALL_BULK_API)
    resp = session.get(SCRYFALL_BULK_API, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    for entry in data.get("data", []):
        if entry.get("type") == "default_cards":
            # 2026-08: Scryfall REMOVED `download_uri` and replaced it with
            # `jsonl_download_uri`, changing the FORMAT in the same move — a
            # gzipped JSON Lines file, not a JSON array. The old key is
            # deliberately NOT accepted as a fallback: it names an array this
            # code can no longer parse, so honouring it would trade a loud
            # failure for a silent mis-parse. Same fix as the app's
            # app/scryfall.py and app/jobs/oracle_ingest.py (v4.13.14); this
            # third copy lives outside that repo and was missed by that sweep.
            url = entry.get("jsonl_download_uri")
            if not url:
                sys.exit(
                    "default_cards has no jsonl_download_uri; keys present: "
                    + str(sorted(entry.keys()))
                )
            log.info("default_cards URL: %s", url)
            return url

    sys.exit("Could not find default_cards in bulk data listing")


def stream_bulk_cards(bulk_path: Path):
    """Yield card dicts from the gzipped JSON Lines bulk file, one line at a time.

    Was `ijson.items(f, "item")` over a JSON array. The bulk export is now
    `.jsonl.gz`, served as `content-type: application/gzip` with NO
    `Content-Encoding`, so nothing in the HTTP stack inflates it — the gzip
    member has to be unwrapped explicitly. Still streaming: one line in memory
    at a time, never the multi-GB whole.
    """
    log.info("Streaming cards from %s", bulk_path)
    opener = gzip.open if str(bulk_path).endswith(".gz") else open
    with opener(bulk_path, "rb") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def main():
    parser = argparse.ArgumentParser(description="Scryfall card image mirror")
    parser.add_argument("--root", required=True, help="Display image root directory")
    parser.add_argument("--art-root", default=None, help="Art crop corpus root (omit to skip)")
    parser.add_argument("--user-agent", required=True, help="User-Agent for Scryfall API requests")
    parser.add_argument("--workers", type=int, default=8, help="Download thread count (default: 8)")
    parser.add_argument(
        "--delay", type=float, default=0.05, help="Per-download delay in seconds (default: 0.05)"
    )
    parser.add_argument("--max", type=int, default=None, help="Max cards to process (for testing)")
    parser.add_argument(
        "--bulk-file",
        default=None,
        help="Use a pre-downloaded bulk .jsonl/.jsonl.gz instead of fetching",
    )
    parser.add_argument("--plan-only", action="store_true", help="Count tasks without downloading")
    args = parser.parse_args()

    root = Path(args.root)
    art_root = Path(args.art_root) if args.art_root else None

    root.mkdir(parents=True, exist_ok=True)
    if art_root:
        art_root.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers["User-Agent"] = args.user_agent
    session.headers["Accept"] = "application/json"

    # Get or use the bulk file
    if args.bulk_file:
        bulk_path = Path(args.bulk_file)
    else:
        bulk_url = get_bulk_download_url(session)
        bulk_path = Path("/tmp/scryfall_default_cards.jsonl.gz")

        if bulk_path.exists() and bulk_path.stat().st_size > 0:
            # Check age: re-download if older than 24 hours
            age_hours = (time.time() - bulk_path.stat().st_mtime) / 3600
            if age_hours < 24:
                log.info("Using cached bulk file (%.1f hours old)", age_hours)
            else:
                log.info("Bulk file is %.1f hours old, re-downloading", age_hours)
                bulk_path.unlink()

        if not bulk_path.exists():
            log.info("Downloading bulk data to %s (this may take a few minutes)...", bulk_path)
            resp = session.get(bulk_url, stream=True, timeout=300)
            resp.raise_for_status()
            fd, tmp = tempfile.mkstemp(suffix=".jsonl.gz.tmp")
            total = 0
            with os.fdopen(fd, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1048576):
                    f.write(chunk)
                    total += len(chunk)
            shutil.move(tmp, str(bulk_path))
            log.info("Bulk data downloaded: %.1f MB", total / 1048576)

    # Build task list
    seen_illustrations = set()
    tasks = []
    card_count = 0
    skipped = 0

    for card in stream_bulk_cards(bulk_path):
        card_count += 1
        for url, dest in extract_download_tasks(card, root, art_root, seen_illustrations):
            if dest.exists() and dest.stat().st_size > 0:
                skipped += 1
            else:
                tasks.append((url, dest))

        if args.max and card_count >= args.max:
            break

        if card_count % 10000 == 0:
            log.info(
                "Scanned %d cards, %d tasks queued, %d skipped (existing)",
                card_count,
                len(tasks),
                skipped,
            )

    log.info(
        "Scan complete: %d cards, %d downloads needed, %d already present",
        card_count,
        len(tasks),
        skipped,
    )

    if args.plan_only:
        log.info("Plan-only mode, exiting")
        return

    if not tasks:
        log.info("Nothing to download")
        return

    # Download with bounded thread pool
    downloaded = 0
    failed = 0
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(download_file, url, dest, session, args.delay): (url, dest)
            for url, dest in tasks
        }

        for future in as_completed(futures):
            url, dest = futures[future]
            try:
                result = future.result()
                if result:
                    downloaded += 1
                if (downloaded + failed) % 1000 == 0:
                    elapsed = time.time() - start_time
                    rate = downloaded / elapsed if elapsed > 0 else 0
                    remaining = (len(tasks) - downloaded - failed) / rate if rate > 0 else 0
                    log.info(
                        "Progress: %d/%d downloaded, %d failed, %.1f/s, ~%.0fm remaining",
                        downloaded,
                        len(tasks),
                        failed,
                        rate,
                        remaining / 60,
                    )
            except Exception as e:
                failed += 1
                log.error("Failed: %s -> %s: %s", url, dest, e)

    elapsed = time.time() - start_time
    log.info(
        "Done: %d downloaded, %d failed, %d skipped, %.1f minutes",
        downloaded,
        failed,
        skipped,
        elapsed / 60,
    )

    if failed > 0:
        log.warning("Re-run to retry failed downloads (skip-if-present makes it safe)")
        sys.exit(1)


if __name__ == "__main__":
    main()
