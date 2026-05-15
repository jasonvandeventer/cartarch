# Screenshots

This folder holds UI captures referenced from the top-level README.

## Captures needed

- `deck-detail.png` — Deck detail page for a representative Commander deck. Show the hero (name, format, bracket badge, totals), Analytics panel (mana curve + types + pips), and the top of the card grid. Best capture: a tagged deck with combos detected so Win Conditions and Synergy panels visibly render.
- `collection.png` — Collection page with a non-trivial filter applied (e.g. `t:creature c:WU cmc:<=3`) so the search syntax is showcased. Mid-scroll, with cards visible, sort dropdown in view.
- `game-tracker.png` — Live game tracker on tablet landscape, 4-player layout, mid-game (life totals diverged, a commander damage cell or two populated).

Optional follow-ups:

- `import-reconciliation.png` — Import preview with reconciliation panel expanded for a deck destination, showing the move-existing / import-new per-row choices.

## Capture settings

- Window width: ~1400px for desktop shots (the page-shell tops out around there).
- DPR: 1× (Retina captures double the file size with no readability gain at the resolutions GitHub renders the README at).
- Format: PNG. Strip metadata before committing (`pngcrush` or equivalent) to keep the repo lean.
- File size target: under 400 KB each.

## Updating the README

The main README's Screenshots section references these by filename:

```markdown
![Deck detail](docs/screenshots/deck-detail.png)
![Collection search](docs/screenshots/collection.png)
```

Drop the files in here and the section will render. No README edit needed unless adding or removing captures.
