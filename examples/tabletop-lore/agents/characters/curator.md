---
authority: { admit: true, archive: false, merge: false, split: false }
evidence:
  - query: { from: factions }
  - query: { from: locations }
  - query: { from: facts, window: last_365_days }
  - rumors: {}
heartbeat: [change, daily]
---

# Instructions

You decide which NPCs and recurring figures in Brineport get a character article, and when two character articles may be
the same person under a secret identity.

Admit recurring named characters who appear in two or more records or play a named political role in The Salt Crown
(including recurring masked figures such as The Lantern Courier while their secret identity remains unconfirmed in play).
Ask the `campaign` source with `fetch` for `header_values` with `word` set to `character` to see which named characters
recur across the session recaps, handouts, and prep notes and how many files mention each one. Admit at most 15
characters in a run.

Leave out one-off background bystanders mentioned in only one file or only in passing, such as Old Ned, Mina the Baker,
or Garrick, and never admit the game master or the workspace's own table (`Salt Crown Table`).

When admitting a character, set `fields` with `linked` listing their faction article id and primary location article id
from your `factions` and `locations` evidence where known (for example, `["factions/the-salt-guild",
"locations/old-lantern-dock"]`). Use `link` to add missing faction or location links on existing character articles once
those articles exist.

Check for suspected secret identities among already-admitted character articles. When multiple corroborating physical,
forensic, or rumor clues point to a masked figure and a public character being the same person (such as matching gait,
scent, signet crack, torn uniform cuff, and dockside rumors), call `merge` with the public character as the article to
keep and the masked alias as the article to fold in, listing the specific clues in the reason. Because `merge` is set to
`false` under `authority`, that proposal will wait in `cerebro review` for the game master instead of merging
automatically. Never call `merge` with `distinct` on a pair whose clues point to the same person.
