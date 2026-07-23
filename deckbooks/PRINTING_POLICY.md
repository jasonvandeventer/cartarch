# Deckbook Printing Recommendation Policy v2

Every card carries **two distinct printing recommendations**. They intentionally
optimise for different goals and should **not converge** unless the same printing
naturally satisfies both. Do not reconcile them into a single pick.

This policy is the source of truth for the ChatGPT briefing export
(`deckbooks/briefing.py` embeds it verbatim) and the deckbook's own labels.

## Definitive printing

> "What version should a player reasonably acquire for this deck?"

Optimise for: deck theme and narrative · artwork appropriateness · cohesion with
the other cards · readability · availability · cost / value.

The Definitive recommendation is the best balance of aesthetics and practicality.
A player should be able to assemble the **entire deck** from Definitive picks
without unreasonable expense. **Do not** automatically recommend the rarest or
most expensive version.

*Curator's note must explain why the printing belongs in the deck.*

## Museum (Collector's Pick)

> "If budget and practicality were irrelevant, what is the most beautiful or
> collectible expression of this card?"

Optimise for: visual impact · premium treatment · collectibility · artistic
execution · prestige. **Ignore deck theme.**

It may be a Secret Lair, a Universes Beyond card, a serialised version, a
Masterpiece, a raised foil, or an expensive promo — whatever is the most desirable
presentation. It is an **aspirational target** that may be **proxied** in the
physical deck while the original stays protected in a collection.

*Curator's note must explain why it represents the pinnacle of the card as an
object of collection.*

## Relationship

The two are **independent**. Sometimes they match:

- Definitive: Future Sight Akroma's Memorial · Museum: Future Sight foil

Often they differ:

- Definitive: original Warstorm Surge · Museum: Marvel Showcase Warstorm Surge

Both outcomes are expected. Never collapse them into one recommendation.
