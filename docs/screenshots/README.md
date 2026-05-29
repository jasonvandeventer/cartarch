# Screenshots

This folder holds UI captures referenced from the top-level README.

## Captures needed

- `deck-detail.jpg` — Deck detail page for a representative Commander deck. Show the hero (name, format, totals), Analytics panel (mana curve + types + pips), Deck Health, and the top of the card grid.
- `collection.jpg` — Collection page with a non-trivial filter applied (e.g. `t:creature c:WU cmc:<=3`) so the search syntax is showcased. Mid-scroll, with cards visible, sort dropdown in view.
- `game-tracker.jpg` — Live game tracker on tablet landscape, 4-player layout, mid-game (life totals diverged, a commander damage cell or two populated).

Optional follow-ups:

- `import-reconciliation.jpg` — Import preview with reconciliation panel expanded for a deck destination, showing the move-existing / import-new per-row choices.

## Capture settings

- Window width: ~1400px for desktop shots (the page-shell tops out around there). Capture at 1× DPR, or downscale a 2× capture to ~1400px wide before committing.
- Crop to just the Cartarch viewport — no browser tab/bookmarks bar, no desktop taskbar/clock.
- Format: JPEG, quality ~92 with 4:4:4 chroma subsampling, metadata stripped. Full color (these shots have smooth gradient backgrounds that band badly as palette PNG). `magick in.png -resize 1400x -strip -sampling-factor 4:4:4 -quality 92 out.jpg`.
- File size target: under ~300 KB each.

## Updating the README

The main README's Screenshots section references these by filename:

```markdown
![Deck detail](docs/screenshots/deck-detail.jpg)
![Collection search](docs/screenshots/collection.jpg)
```

Drop the files in here and the section will render. No README edit needed unless adding or removing captures.
