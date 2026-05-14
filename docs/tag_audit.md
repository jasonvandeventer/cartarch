# Tag Auto-Tagger Audit

Audit run for **jason@vanfreckle.com** across 4 decks (157 unique non-basic cards total, 100 sampled).

**How to use this file:**

1. For each row, compare the **Auto-tagger output** to the **Expected tags** you write in. Use docs/tag_system_overhaul.md §2.1 as the rule reference.
2. Fill in the **Mark** column with one of: `correct`, `false-pos`, `false-neg`, `partial`.
3. Add brief **Notes** for any case that informs a pattern change (§2.3) or a theme addition (§2.4).
4. Findings drive the code changes that follow this audit. Don't skip rows — a blank Mark is treated as unreviewed.

**Buckets:**

- known-mistagged: 8
- commander-ambiguous: 20
- complex-oracle: 15
- random remainder: 57

---

## 1. Faramir, Field Commander · LTR #303 · _known-mistagged_

- **Type:** Legendary Creature — Human Soldier
- **Auto-tagger (intrinsic):** Draw
- **Auto-tagger (themes-aware):** **Teysa Karlov**: Draw, Synergy
- **Current user tags:** Draw
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 2. Solemn Simulacrum · WHO #246 · _known-mistagged_

- **Type:** Artifact Creature — Golem
- **Auto-tagger (intrinsic):** Ramp, Draw
- **Auto-tagger (themes-aware):** **Teysa Karlov**: Ramp, Draw, Synergy
- **Current user tags:** Draw, Ramp
- **EDHREC:** **Teysa Karlov**: synergy +0.25; in 40.2% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 3. Serrated Scorpion · CMM #185 · _known-mistagged_

- **Type:** Creature — Scorpion
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** **Teysa Karlov**: Synergy
- **Current user tags:** Wipe
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 4. Promise of Loyalty · BLC #148 · _known-mistagged_

- **Type:** Sorcery
- **Auto-tagger (intrinsic):** Wipe
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Wipe
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 5. Syr Konrad, the Grim · M3C #207 · _known-mistagged_

- **Type:** Legendary Creature — Human Knight
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** **Teysa Karlov**: Synergy
- **Current user tags:** Wipe
- **EDHREC:** **Teysa Karlov**: synergy +0.37; in 45.1% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 6. Demon's Disciple · CMM #149 · _known-mistagged_

- **Type:** Creature — Human Cleric
- **Auto-tagger (intrinsic):** Removal
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Wipe
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 7. Plaguecrafter · BLC #187 · _known-mistagged_

- **Type:** Creature — Human Shaman
- **Auto-tagger (intrinsic):** Removal
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Wipe
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 8. Echoing Assault · BLC #24 · _known-mistagged_

- **Type:** Enchantment
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** **Bello**: Synergy
- **Current user tags:** Synergy
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.10; in 11.8% of decks; list=Enchantments
- **Expected tags:**
- **Mark:**
- **Notes:**

## 9. Gratuitous Violence · BLC #197 · _commander-ambiguous_

- **Type:** Enchantment
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** **Bello**: Synergy
- **Current user tags:** Synergy
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.71; in 80.6% of decks; list=High Synergy Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 10. Berserkers' Onslaught · BLC #192 · _commander-ambiguous_

- **Type:** Enchantment
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** **Bello**: Synergy
- **Current user tags:** Synergy
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.78; in 88.6% of decks; list=High Synergy Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 11. Victimize · MH3 #278 · _commander-ambiguous_

- **Type:** Sorcery
- **Auto-tagger (intrinsic):** Engine
- **Auto-tagger (themes-aware):** **Teysa Karlov**: Engine, Synergy
- **Current user tags:** Engine, Synergy
- **EDHREC:** **Teysa Karlov**: synergy +0.32; in 49.2% of decks; list=Top Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 12. Sifter of Skulls · M3C #203 · _commander-ambiguous_

- **Type:** Creature — Eldrazi
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** **Teysa Karlov**: Synergy
- **Current user tags:** Ramp
- **EDHREC:** **Teysa Karlov**: synergy +0.30; in 33.0% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 13. Ashnod's Altar · EMA #218 · _commander-ambiguous_

- **Type:** Artifact
- **Auto-tagger (intrinsic):** Ramp, Engine
- **Auto-tagger (themes-aware):** **Teysa Karlov**: Ramp, Engine, Synergy
- **Current user tags:** Ramp
- **EDHREC:** **Teysa Karlov**: synergy +0.51; in 69.0% of decks; list=Top Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 14. Carrion Feeder · PLST #MH1-81 · _commander-ambiguous_

- **Type:** Creature — Zombie
- **Auto-tagger (intrinsic):** Engine
- **Auto-tagger (themes-aware):** **Teysa Karlov**: Engine, Synergy
- **Current user tags:** Engine, Synergy
- **EDHREC:** **Teysa Karlov**: synergy +0.38; in 46.9% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 15. Rain of Riches · BLC #200 · _commander-ambiguous_

- **Type:** Enchantment
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** **Bello**: Ramp, Synergy
- **Current user tags:** Ramp, Synergy
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.28; in 33.1% of decks; list=Enchantments
- **Expected tags:**
- **Mark:**
- **Notes:**

## 16. Rolling Hamsphere · BLC #39 · _commander-ambiguous_

- **Type:** Artifact — Vehicle
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** **Bello**: Synergy
- **Current user tags:** Synergy
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.49; in 54.1% of decks; list=Top Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 17. Primeval Bounty · BLC #232 · _commander-ambiguous_

- **Type:** Enchantment
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** **Bello**: Synergy
- **Current user tags:** Synergy
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.43; in 50.7% of decks; list=Enchantments
- **Expected tags:**
- **Mark:**
- **Notes:**

## 18. Sunbird's Invocation · BLC #116 · _commander-ambiguous_

- **Type:** Enchantment
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** **Bello**: Synergy
- **Current user tags:** Synergy
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.68; in 76.6% of decks; list=High Synergy Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 19. Silverquill Lecturer · M3C #96 · _commander-ambiguous_

- **Type:** Creature — Kor Wizard
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** **Teysa Karlov**: Synergy
- **Current user tags:** Synergy
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 20. Warstorm Surge · BLC #117 · _commander-ambiguous_

- **Type:** Enchantment
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** **Bello**: Synergy
- **Current user tags:** Synergy
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.59; in 72.8% of decks; list=High Synergy Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 21. Shadowheart, Dark Justiciar · CLB #146 · _commander-ambiguous_

- **Type:** Legendary Creature — Human Elf Cleric
- **Auto-tagger (intrinsic):** Draw, Engine
- **Auto-tagger (themes-aware):** **Teysa Karlov**: Draw, Engine, Synergy
- **Current user tags:** Draw
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 22. Reassembling Skeleton · CMM #183 · _commander-ambiguous_

- **Type:** Creature — Skeleton Warrior
- **Auto-tagger (intrinsic):** Engine
- **Auto-tagger (themes-aware):** **Teysa Karlov**: Engine, Synergy
- **Current user tags:** Engine, Synergy
- **EDHREC:** **Teysa Karlov**: synergy +0.51; in 60.1% of decks; list=High Synergy Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 23. Unnatural Growth · BLC #245 · _commander-ambiguous_

- **Type:** Enchantment
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** **Bello**: Synergy
- **Current user tags:** Synergy
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.65; in 89.6% of decks; list=High Synergy Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 24. Evin, Waterdeep Opportunist · SLD #1239 · _commander-ambiguous_

- **Type:** Legendary Creature — Human Rogue
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** **Teysa Karlov**: Ramp, Synergy
- **Current user tags:** Ramp
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 25. Doomed Traveler · ISD #11 · _commander-ambiguous_

- **Type:** Creature — Human Soldier
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** **Teysa Karlov**: Synergy
- **Current user tags:** Synergy
- **EDHREC:** **Teysa Karlov**: synergy +0.51; in 56.9% of decks; list=High Synergy Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 26. Luminous Broodmoth · BLC #144 · _commander-ambiguous_

- **Type:** Creature — Insect
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** **Teysa Karlov**: Synergy
- **Current user tags:** Synergy
- **EDHREC:** **Teysa Karlov**: synergy +0.39; in 47.3% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 27. Idol of Oblivion · BLC #277 · _commander-ambiguous_

- **Type:** Artifact
- **Auto-tagger (intrinsic):** Draw
- **Auto-tagger (themes-aware):** **Teysa Karlov**: Draw, Synergy
- **Current user tags:** Draw
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 28. Greater Good · BLC #223 · _commander-ambiguous_

- **Type:** Enchantment
- **Auto-tagger (intrinsic):** Engine
- **Auto-tagger (themes-aware):** **Bello**: Engine, Synergy
- **Current user tags:** Engine, Synergy
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.31; in 40.0% of decks; list=Enchantments
- **Expected tags:**
- **Mark:**
- **Notes:**

## 29. Farewell · M3C #170 · _complex-oracle_

- **Type:** Sorcery
- **Auto-tagger (intrinsic):** Wipe, Hate
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Wipe
- **EDHREC:** **Teysa Karlov**: synergy -0.04; in 7.7% of decks; list=Game Changers
- **Expected tags:**
- **Mark:**
- **Notes:**

## 30. Final Act · M3C #104 · _complex-oracle_

- **Type:** Sorcery
- **Auto-tagger (intrinsic):** Wipe, Hate
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Wipe
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 31. Twinflame Tyrant · FDN #333 · _complex-oracle_

- **Type:** Creature — Dragon
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 32. Insatiable Avarice · OTJ #91 · _complex-oracle_

- **Type:** Sorcery
- **Auto-tagger (intrinsic):** Draw, Tutor
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Draw, Tutor
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 33. Bello, Bard of the Brambles · BLC #1 · _complex-oracle_

- **Type:** Legendary Creature — Raccoon Bard
- **Auto-tagger (intrinsic):** Draw
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Draw
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 34. Raging Ravine · BLC #324 · _complex-oracle_

- **Type:** Land
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.33; in 42.8% of decks; list=Utility Lands
- **Expected tags:**
- **Mark:**
- **Notes:**

## 35. Akroma's Will · M3C #165 · _complex-oracle_

- **Type:** Instant
- **Auto-tagger (intrinsic):** Protection
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Protection
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 36. Austere Command · M3C #167 · _complex-oracle_

- **Type:** Sorcery
- **Auto-tagger (intrinsic):** Wipe
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Wipe
- **EDHREC:** **Teysa Karlov**: synergy -0.04; in 13.4% of decks; list=Sorceries
- **Expected tags:**
- **Mark:**
- **Notes:**

## 37. Big Score · BLC #193 · _complex-oracle_

- **Type:** Instant
- **Auto-tagger (intrinsic):** Ramp, Draw
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Draw, Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.19; in 28.0% of decks; list=Instants
- **Expected tags:**
- **Mark:**
- **Notes:**

## 38. Brightcap Badger // Fungus Frolic · BLC #62 · _complex-oracle_

- **Type:** Creature — Badger Druid // Instant — Adventure
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 39. Tragic Slip · CMM #192 · _complex-oracle_

- **Type:** Instant
- **Auto-tagger (intrinsic):** Removal
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Removal
- **EDHREC:** **Teysa Karlov**: synergy +0.09; in 13.0% of decks; list=Instants
- **Expected tags:**
- **Mark:**
- **Notes:**

## 40. Popular Egotist · DSK #114 · _complex-oracle_

- **Type:** Creature — Human Rogue
- **Auto-tagger (intrinsic):** Protection
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Protection
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 41. Grothama, All-Devouring · BLC #224 · _complex-oracle_

- **Type:** Legendary Creature — Wurm
- **Auto-tagger (intrinsic):** Draw
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Draw
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.18; in 20.2% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 42. Abrade · BLC #191 · _complex-oracle_

- **Type:** Instant
- **Auto-tagger (intrinsic):** Removal
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Removal
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.30; in 46.1% of decks; list=Instants
- **Expected tags:**
- **Mark:**
- **Notes:**

## 43. Leyline of the Void · WOT #30 · _complex-oracle_

- **Type:** Enchantment
- **Auto-tagger (intrinsic):** Hate
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Hate
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 44. Wandertale Mentor · BLB #240 · _random_

- **Type:** Creature — Raccoon Bard
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.45; in 53.5% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 45. Starstorm · BLC #203 · _random_

- **Type:** Instant
- **Auto-tagger (intrinsic):** Draw
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Draw
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.30; in 34.9% of decks; list=Instants
- **Expected tags:**
- **Mark:**
- **Notes:**

## 46. Cultivate · BLC #212 · _random_

- **Type:** Sorcery
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.12; in 61.4% of decks; list=Top Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 47. Path to Exile · PF20 #1 · _random_

- **Type:** Instant
- **Auto-tagger (intrinsic):** Removal
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Removal
- **EDHREC:** **Teysa Karlov**: synergy +0.03; in 52.2% of decks; list=Top Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 48. Kodama of the East Tree · BLC #227 · _random_

- **Type:** Legendary Creature — Spirit
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.47; in 54.4% of decks; list=Top Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 49. Minthara, Merciless Soul · CLB #286 · _random_

- **Type:** Legendary Creature — Elf Cleric
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 50. Fellwar Stone · BLC #269 · _random_

- **Type:** Artifact
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.29; in 39.1% of decks; list=Mana Artifacts
- **Expected tags:**
- **Mark:**
- **Notes:**

## 51. Evercoat Ursine · BLC #30 · _random_

- **Type:** Creature — Elemental Bear
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.19; in 21.6% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 52. Llanowar Loamspeaker · BLC #228 · _random_

- **Type:** Creature — Elf Druid
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.20; in 23.5% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 53. Garruk's Packleader · BLC #218 · _random_

- **Type:** Creature — Beast
- **Auto-tagger (intrinsic):** Draw
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Draw
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.25; in 34.9% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 54. Sheltered Thicket · BLC #330 · _random_

- **Type:** Land — Mountain Forest
- **Auto-tagger (intrinsic):** Draw
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Draw
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.32; in 54.0% of decks; list=Lands
- **Expected tags:**
- **Mark:**
- **Notes:**

## 55. Ruby, Daring Tracker · FDN #245 · _random_

- **Type:** Legendary Creature — Human Scout
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 56. Gruul Turf · BLC #310 · _random_

- **Type:** Land
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.21; in 58.3% of decks; list=Lands
- **Expected tags:**
- **Mark:**
- **Notes:**

## 57. Grumgully, the Generous · BLC #253 · _random_

- **Type:** Legendary Creature — Goblin Shaman
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.10; in 16.7% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 58. Beast Within · BLC #206 · _random_

- **Type:** Instant
- **Auto-tagger (intrinsic):** Removal
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Removal
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.26; in 78.6% of decks; list=Top Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 59. Mangara, the Diplomat · BLC #145 · _random_

- **Type:** Legendary Creature — Human Cleric
- **Auto-tagger (intrinsic):** Draw
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Draw
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 60. Vashta Nerada · WHO #73 · _random_

- **Type:** Creature — Alien Horror
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 61. Mind Stone · BLC #280 · _random_

- **Type:** Artifact
- **Auto-tagger (intrinsic):** Ramp, Draw
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Draw, Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.26; in 32.9% of decks; list=Mana Artifacts
- **Expected tags:**
- **Mark:**
- **Notes:**

## 62. Copperline Gorge · ONE #371 · _random_

- **Type:** Land
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.43; in 61.1% of decks; list=Lands
- **Expected tags:**
- **Mark:**
- **Notes:**

## 63. Thornspire Verge · DSK #270 · _random_

- **Type:** Land
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.03; in 26.3% of decks; list=Lands
- **Expected tags:**
- **Mark:**
- **Notes:**

## 64. Game Trail · BLC #306 · _random_

- **Type:** Land
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.26; in 71.1% of decks; list=Lands
- **Expected tags:**
- **Mark:**
- **Notes:**

## 65. Toxic Deluge · LTC #209 · _random_

- **Type:** Sorcery
- **Auto-tagger (intrinsic):** Wipe
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Wipe
- **EDHREC:** **Teysa Karlov**: synergy +0.05; in 22.4% of decks; list=Sorceries
- **Expected tags:**
- **Mark:**
- **Notes:**

## 66. Sol Ring · WHO #245 · _random_

- **Type:** Artifact
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.14; in 92.1% of decks; list=Mana Artifacts
- **Expected tags:**
- **Mark:**
- **Notes:**

## 67. Decimate · BLC #251 · _random_

- **Type:** Sorcery
- **Auto-tagger (intrinsic):** Removal
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Removal
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.26; in 48.5% of decks; list=Sorceries
- **Expected tags:**
- **Mark:**
- **Notes:**

## 68. Trailtracker Scout · BLC #35 · _random_

- **Type:** Creature — Raccoon Scout
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.55; in 63.5% of decks; list=High Synergy Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 69. Rampant Growth · BLC #234 · _random_

- **Type:** Sorcery
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.24; in 67.5% of decks; list=Top Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 70. Bojuka Bog · LTC #358 · _random_

- **Type:** Land
- **Auto-tagger (intrinsic):** Hate
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Hate
- **EDHREC:** **Teysa Karlov**: synergy +0.03; in 47.2% of decks; list=Utility Lands
- **Expected tags:**
- **Mark:**
- **Notes:**

## 71. Thought Vessel · BLC #289 · _random_

- **Type:** Artifact
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.47; in 57.4% of decks; list=Mana Artifacts
- **Expected tags:**
- **Mark:**
- **Notes:**

## 72. Reliquary Tower · WHO #296 · _random_

- **Type:** Land
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Teysa Karlov**: synergy +0.06; in 29.2% of decks; list=Utility Lands
- **Expected tags:**
- **Mark:**
- **Notes:**

## 73. Rampaging Baloths · BLC #233 · _random_

- **Type:** Creature — Beast
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.12; in 22.4% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 74. Myriad Landscape · M3C #358 · _random_

- **Type:** Land
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Teysa Karlov**: synergy -0.02; in 16.9% of decks; list=Lands
- **Expected tags:**
- **Mark:**
- **Notes:**

## 75. Blind Obedience · WOT #1 · _random_

- **Type:** Enchantment
- **Auto-tagger (intrinsic):** Hate
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Hate
- **EDHREC:** **Teysa Karlov**: synergy -0.11; in 6.6% of decks; list=Enchantments
- **Expected tags:**
- **Mark:**
- **Notes:**

## 76. Caves of Koilos · M3C #328 · _random_

- **Type:** Land
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Teysa Karlov**: synergy +0.06; in 63.4% of decks; list=Lands
- **Expected tags:**
- **Mark:**
- **Notes:**

## 77. Path of Ancestry · BLC #322 · _random_

- **Type:** Land
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.26; in 43.9% of decks; list=Lands
- **Expected tags:**
- **Mark:**
- **Notes:**

## 78. Mosswort Bridge · BLC #317 · _random_

- **Type:** Land
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.39; in 65.2% of decks; list=Utility Lands
- **Expected tags:**
- **Mark:**
- **Notes:**

## 79. Pyreswipe Hawk · BLC #26 · _random_

- **Type:** Creature — Elemental Bird
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.34; in 37.7% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 80. Harmonize · BLC #120 · _random_

- **Type:** Sorcery
- **Auto-tagger (intrinsic):** Draw
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Draw
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.10; in 27.2% of decks; list=Sorceries
- **Expected tags:**
- **Mark:**
- **Notes:**

## 81. Sakura-Tribe Elder · BLC #236 · _random_

- **Type:** Creature — Snake Shaman
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.24; in 48.1% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 82. Wooded Ridgeline · BLC #353 · _random_

- **Type:** Land — Mountain Forest
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.30; in 43.3% of decks; list=Lands
- **Expected tags:**
- **Mark:**
- **Notes:**

## 83. Burnished Hart · C21 #238 · _random_

- **Type:** Artifact Creature — Elk
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.07; in 9.1% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 84. Etali, Primal Storm · BLC #196 · _random_

- **Type:** Legendary Creature — Elder Dinosaur
- **Auto-tagger (intrinsic):** Draw
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Draw
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.04; in 19.9% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 85. Eldrazi Monument · M3C #290 · _random_

- **Type:** Artifact
- **Auto-tagger (intrinsic):** Protection
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Protection
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 86. Cinder Glade · BLC #299 · _random_

- **Type:** Land — Mountain Forest
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.18; in 85.7% of decks; list=Lands
- **Expected tags:**
- **Mark:**
- **Notes:**

## 87. Goreclaw, Terror of Qal Sisma · BLC #222 · _random_

- **Type:** Legendary Creature — Bear
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.17; in 32.6% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 88. Selfless Spirit · BLC #153 · _random_

- **Type:** Creature — Spirit Cleric
- **Auto-tagger (intrinsic):** Protection
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Protection
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 89. Ghalta, Primal Hunger · BLC #220 · _random_

- **Type:** Legendary Creature — Elder Dinosaur
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.19; in 31.3% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 90. Tendershoot Dryad · BLC #242 · _random_

- **Type:** Creature — Dryad
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.19; in 21.4% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 91. Orcish Bowmasters · LTR #433 · _random_

- **Type:** Creature — Orc Archer
- **Auto-tagger (intrinsic):** Removal, Hate
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Removal
- **EDHREC:** **Teysa Karlov**: synergy +0.02; in 7.5% of decks; list=Game Changers
- **Expected tags:**
- **Mark:**
- **Notes:**

## 92. Blasphemous Act · BLC #114 · _random_

- **Type:** Sorcery
- **Auto-tagger (intrinsic):** Ramp, Wipe
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp, Wipe
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.42; in 86.1% of decks; list=Top Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 93. Farseek · BLC #119 · _random_

- **Type:** Sorcery
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.38; in 69.8% of decks; list=Top Cards
- **Expected tags:**
- **Mark:**
- **Notes:**

## 94. Lotus Cobra · BLC #229 · _random_

- **Type:** Creature — Snake
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.21; in 30.8% of decks; list=Creatures
- **Expected tags:**
- **Mark:**
- **Notes:**

## 95. Sol Ring · PF19 #7 · _random_

- **Type:** Artifact
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Teysa Karlov**: synergy +0.06; in 90.1% of decks; list=Mana Artifacts
- **Expected tags:**
- **Mark:**
- **Notes:**

## 96. Vesuva · M3C #404 · _random_

- **Type:** Land
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 97. Quilled Greatwurm · FDN #111 · _random_

- **Type:** Creature — Wurm
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**

## 98. Command Tower · BLC #130 · _random_

- **Type:** Land
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.12; in 94.9% of decks; list=Lands
- **Expected tags:**
- **Mark:**
- **Notes:**

## 99. Terramorphic Expanse · BLC #345 · _random_

- **Type:** Land
- **Auto-tagger (intrinsic):** Ramp
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** Ramp
- **EDHREC:** **Bello** (Bello, Bard of the Brambles): synergy +0.20; in 38.8% of decks; list=Lands
- **Expected tags:**
- **Mark:**
- **Notes:**

## 100. Restless Fortress · WOE #259 · _random_

- **Type:** Land
- **Auto-tagger (intrinsic):** —
- **Auto-tagger (themes-aware):** (same as intrinsic)
- **Current user tags:** —
- **EDHREC:** **Teysa Karlov**: not surfaced on EDHREC
- **Expected tags:**
- **Mark:**
- **Notes:**
