# Tabletop Lore: The Salt Crown

Keep canon, faction ledgers, oaths, betrayals, and dockside gossip straight across a 20-session tabletop RPG campaign. This workspace is for a game master and players running *The Salt Crown*, a political intrigue campaign set in the free city of Brineport. It builds character dossiers, faction briefings, location guides, and a pre-session recap from session notes, in-world handouts, GM prep files, a CSV faction ledger, and live rumors logged over MCP.

## What is in the folder

```text
examples/tabletop-lore/
├── settings.yaml              # models, daily budget, and time zone
├── workspace.md               # campaign brief read by every agent run
├── profile.md                 # game master identity
├── extensions.py              # loads the bundled Rumors app
├── apps/rumors/app.py         # custom rumor record kind, log_rumor MCP tool, and rumors evidence
├── facts/kinds.py             # Oath, Betrayal, Rumor, Secret, ItemTransfer, and Other
├── sources/
│   ├── campaign.yaml          # reads sessions/, handouts/, and prep/ markdown files
│   └── ledger.yaml            # reads data/faction-ledger.csv rows into factions
├── sessions/                  # six session recaps (Sessions 08 through 13)
├── handouts/                  # broadsheets, writs, sealed letters, and transfer orders
├── prep/                      # GM prep notes with Faction, Location, and Character headers
├── data/faction-ledger.csv    # faction standing and treasury ledger across sessions
├── agents/
│   ├── characters/            # curator (admit: true, merge: false) plus dossier and whispers subagents
│   ├── factions/              # curator reading held CSV ledger rows plus faction writer
│   ├── locations/             # curator fetching Location headers plus location writer
│   └── digests/session-brief.md
└── evals/continuity.yaml      # six continuity checks scored by cerebro eval
```

## Run it

Install Cerebro from the repository root, trust a copy of this workspace with a fresh id, and set your two model keys.

```sh
uv tool install .
cerebro trust --new-id examples/tabletop-lore
cerebro secrets set fireworks --workspace examples/tabletop-lore
cerebro secrets set typesafe --workspace examples/tabletop-lore
```

Run the first pass to discover the factions, locations, and recurring characters, then compile the initial wiki.

```sh
cerebro serve --once --yes --workspace examples/tabletop-lore
```

Log an in-session tavern rumor through the bundled `rumors` app, then run another pass so the new whisper reaches character, location, and digest articles.

```sh
cerebro tools log_rumor --workspace examples/tabletop-lore --json \
  '{"speaker": "Dockhand Tamsin", "location": "Old Lantern Dock", "about": "Captain Marlo Vance and The Lantern Courier", "claim": "Captain Marlo Vance came back to his watch house just after the dawn raid wearing a harbour-blue dress coat missing its left braided cuff, limping hard on his left boot and reeking of smoked brine-tar, just like The Lantern Courier."}'
cerebro serve --once --workspace examples/tabletop-lore
```

Check `cerebro review --workspace examples/tabletop-lore` to inspect the waiting merge proposal that links The Lantern Courier to Captain Marlo Vance without spoiling or auto-merging the secret identity. Run the continuity eval suite to verify key campaign facts.

```sh
cerebro eval evals/continuity.yaml --workspace examples/tabletop-lore
```

## What it shows

| Feature | Where to look | What it does here |
|---|---|---|
| Custom app, `@mcp_tool`, and `@evidence` | `apps/rumors/app.py`, `extensions.py` | Declares record kind `rumor`, exposes `log_rumor(speaker, claim, location)`, and feeds distilled rumor facts into character, location, and digest writers |
| Selective curator authority | `agents/characters/curator.md` | Uses `authority: { admit: true, merge: false }` so recurring NPCs are admitted while suspected secret-identity merges pause in `cerebro review` |
| Subagents composing parts | `agents/characters/article.md`, `agents/characters/subagents/` | Splits each character article into `dossier` (confirmed canon) and `whispers` (secrets and unverified rumors) |
| Front-matter `override` fields | `agents/characters/subagents/dossier.md`, `agents/factions/article.md` | Sets `status_in_world` (`alive`, `missing`, or `imprisoned`), `allegiance`, `standing`, and `treasury_gp` from the latest facts |
| Linked articles across collections | `agents/characters/curator.md`, `agents/locations/curator.md`, `agents/factions/article.md` | Connects `characters`, `factions`, and `locations` with `linked` fields and loads related articles with `linked: this` and `articles: [this.linked]` |
| CSV row source | `sources/ledger.yaml`, `data/faction-ledger.csv` | Turns faction ledger rows into held records and field facts for standing, treasury, and lead NPC |
| Custom fact kinds | `facts/kinds.py` | Extracts `Oath`, `Betrayal`, `Rumor`, `Secret`, `ItemTransfer`, and `Other` facts so writers can filter by kind |
| Eval suite | `evals/continuity.yaml` | Checks six continuity questions, including an amended transfer date and a cleared theft rumor |

## Sample article from a real run

In a two-pass run on Fireworks `glm-5p3` and `glm-5p3-flash` with Typesafe `jev-1.13.0` (including three rumors logged with `log_rumor` and the six-question eval suite), Cerebro read **24 records**, extracted **126 current facts**, wrote **15 articles** (6 characters, 4 factions, 4 locations, and 1 session brief), scored **1.00** on `evals/continuity.yaml`, and filed a waiting `article.merge` proposal for `characters/captain-marlo-vance` and `characters/the-lantern-courier` in `cerebro review`. Total spend was **$2.38** ($0.05 extraction, $2.33 compilation, and $0.0007 evaluation).

Below is a trimmed copy of `articles/characters/captain-marlo-vance.md` from that run, showing the `override` front matter, `linked` location, confirmed canon under `Dossier`, and the logged dockside rumor under `Whispers`.

```markdown
---
id: characters/captain-marlo-vance
title: Captain Marlo Vance
identity:
  character:
    - Captain Marlo Vance
allegiance: unaligned
linked:
  - locations/old-lantern-dock
status_in_world: alive
compiled_at: '2026-11-27T02:41:47Z'
---

## Dossier

Captain Marlo Vance is the Harbor Master of Old Lantern Dock, administering the pier under municipal charter rather than under any faction's banner [^fact_01m8ddpgtrjfcxfcb6] [^fact_01m8ddpqxa4a0r5ffa]. His watch house sits where it overlooks both the chain-boom and the tide-bell door [^fact_01m8ddpqxajg9zvmyb], and he remained alive and at the dock after the Salt Guild raid of 2026-11-20 [^fact_01m8ddqfhcx054fmch].

On 2026-09-25 Vance met the crew at the timber pilings and inspected their manifest by storm-lantern, and that same day he publicly reaffirmed the Harbor Master's Neutrality Oath before the dockhands and the party, swearing that Old Lantern Dock would keep its berths open to every vessel regardless of guild seal [^fact_01m8ddq0cdnyzht51q] [^fact_01m8ddq0cdv015e48a].

The 2026-11-14 Closed-Berth Writ ordered Vance, as Harbor Master, to surrender the Brass Harbour Key to Salt Guild wardens upon inspection [^fact_01m8ddpjq108bygez7]. He has not surrendered it: he remains at his post at Old Lantern Dock under guild supervision, having delayed handing over the key by citing an old municipal audit clause [^fact_01m8ddpmqzv3150jh5], and Salt Guild bailiffs now watch his office day and night [^fact_01m8ddqfk9yezq3yn9]. Magistrate Vespera Corwin has ordered Salt Guild bailiffs to strip Vance of the Brass Harbour Key at Old Lantern Dock if he refuses to surrender it by 2026-11-30 [^fact_01m8ddpy1brts3qdm0].

## Whispers

### The Lantern Courier identity — clues only, unresolved

The matching clues noted in the evidence: both The Lantern Courier and Captain Marlo Vance walk with a distinct limp in the left boot [^fact_01m8ddpt0c3bbwwp97], and both smell strongly of the smoked brine-tar used to waterproof harbor boots [^fact_01m8ddpt0cx3914z3x]. The Lantern Courier's Session 10 crimson wax scrap shows the same crescent crack on the left rim that Captain Marlo Vance describes on his own iron signet ring in his 2026-11-08 letter to Brother Aldric Fen [^fact_01m8ddpt0c0e56pga0].

### Unverified dockside rumor — Dockhand Tamsin

Unverified rumor heard at Old Lantern Dock from Dockhand Tamsin: Captain Marlo Vance came back to his watch house just after the dawn raid [^fact_01m8de2ghrsry3bqfd]. In the same rumor, Dockhand Tamsin says Vance was wearing a harbour-blue dress coat missing its left braided cuff [^fact_01m8de2gm9r7essqss] and was limping hard on his left boot [^fact_01m8de2gpbkyg7kmk3].

[^fact_01m8ddpgtrjfcxfcb6]: handouts/2026-11-08-marlo-sealed-letter.md "# Sealed Harbor Dispatch — Marlo Vance to Brother Aldric Fen … Yours in quiet watch, Captain Marlo Vance, Harbor Master"
[^fact_01m8ddpqxa4a0r5ffa]: prep/2026-10-02-old-lantern-dock-dossier.md "Officially administered by Harbor Master Captain Marlo Vance under municipal charter, its underside hides a tidal smuggling sluice beneath Pier 4 used by The Tidebreakers to move contraband past guild toll-boats."
[^fact_01m8ddpqxajg9zvmyb]: prep/2026-10-02-old-lantern-dock-dossier.md "Captain Marlo Vance's watch house overlooks both the chain-boom and the tide-bell door."
[^fact_01m8ddqfhcx054fmch]: sessions/2026-11-20-session-13.md "Captain Marlo Vance remained alive at Old Lantern Dock after the raid, though guild bailiffs now watch his office day and night."
[^fact_01m8ddq0cdnyzht51q]: sessions/2026-09-25-session-08.md "Harbor Master Captain Marlo Vance met the crew at the timber pilings and inspected their manifest by storm-lantern."
[^fact_01m8ddq0cdv015e48a]: sessions/2026-09-25-session-08.md "Captain Marlo Vance publicly reaffirmed the Harbor Master's Neutrality Oath, swearing before the dockhands and the party that Old Lantern Dock would keep its berths open to every vessel regardless of guild seal."
[^fact_01m8ddpjq108bygez7]: handouts/2026-11-14-closed-berth-writ.md "No vessel may berth without a guild manifest stamped at The Salt Exchange, and Harbor Master Captain Marlo Vance is ordered to surrender the Brass Harbour Key to Salt Guild wardens upon inspection."
[^fact_01m8ddpmqzv3150jh5]: handouts/2026-11-21-brineport-crier-issue-47.md "**HARBOR MASTER UNDER WATCH.** Captain Marlo Vance remains at his post at Old Lantern Dock under guild supervision, having delayed surrendering the Brass Harbour Key by citing an old municipal audit clause."
[^fact_01m8ddqfk9yezq3yn9]: sessions/2026-11-20-session-13.md "Captain Marlo Vance remained alive at Old Lantern Dock after the raid, though guild bailiffs now watch his office day and night."
[^fact_01m8ddpy1brts3qdm0]: prep/2026-11-26-session-14-prep.md "Heading into Session 14 on 2026-11-27, three threats are converging: - Alderman Bramwell Oakhart remains imprisoned in Cell 4 at Brine Citadel; his transfer to High Reef Quarry moved from 2026-11-29 to 2026-12-03, giving the party six days to plan a rescue or overturn the writ. - Former Compact…"
[^fact_01m8ddpt0c3bbwwp97]: prep/2026-11-10-lantern-courier-clues.md "Both The Lantern Courier and Captain Marlo Vance walk with a distinct limp in the left boot."
[^fact_01m8ddpt0cx3914z3x]: prep/2026-11-10-lantern-courier-clues.md "Unverified rumor heard at Old Lantern Dock from Dockhand Tamsin regarding Captain Marlo Vance and The Lantern Courier: Captain Marlo Vance came back to his watch house just after the dawn raid wearing a harbour-blue dress coat missing its left braided cuff, limping hard on his left boot and reeking…"
[^fact_01m8ddpt0c0e56pga0]: prep/2026-11-10-lantern-courier-clues.md "3. The Lantern Courier's Session 10 crimson wax scrap shows the same crescent crack on the left rim that Captain Marlo Vance describes on his own iron signet ring in his 2026-11-08 letter to Brother Aldric Fen."
[^fact_01m8de2ghrsry3bqfd]: rumor_717f69a6f54e "Unverified rumor heard at Old Lantern Dock from Dockhand Tamsin regarding Captain Marlo Vance and The Lantern Courier: Captain Marlo Vance came back to his watch house just after the dawn raid wearing a harbour-blue dress coat missing its left braided cuff, limping hard on his left boot and reeking…"
[^fact_01m8de2gm9r7essqss]: rumor_717f69a6f54e "Unverified rumor heard at Old Lantern Dock from Dockhand Tamsin regarding Captain Marlo Vance and The Lantern Courier: Captain Marlo Vance came back to his watch house just after the dawn raid wearing a harbour-blue dress coat missing its left braided cuff, limping hard on his left boot and reeking…"
[^fact_01m8de2gpbkyg7kmk3]: rumor_717f69a6f54e "Unverified rumor heard at Old Lantern Dock from Dockhand Tamsin regarding Captain Marlo Vance and The Lantern Courier: Captain Marlo Vance came back to his watch house just after the dawn raid wearing a harbour-blue dress coat missing its left braided cuff, limping hard on his left boot and reeking…"
```
