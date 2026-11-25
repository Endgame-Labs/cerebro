# Tell

Tell the wiki something it should know, or ask for a change to an article, in plain words. Cerebro keeps what you said as a record, turns what it states into facts, and turns what it asks for into proposals. Your agent can do the same over MCP. The examples here use the `team-wiki` from the [quickstart](quickstart.md).

## Tell it a fact

From the command line, `cerebro tools tell` tells the wiki as you, the workspace's owner:

```text
$ cerebro tools tell --json '{"text": "The Atlas launch moved again, to 16 December.", "target": "projects/atlas", "as": "fact"}'
{
  "record": "r_f80dbd52d80e",
  "facts": [
    "fact_hz1htqj82h33thwx0e"
  ]
}
```

The words are kept whole as the record `r_f80dbd52d80e`, and the fact links to Atlas. Atlas's agent sees the fact on its next run and rewrites the article, as `cerebro serve --once` shows:

```text
due       projects/atlas: change
wrote     projects/atlas  articles/projects/atlas.md
```

A tell never edits an article itself. The article's agents read what you told them and write it in.

From an MCP client, your agent sends the same arguments to the `tell` tool:

```text
tell { "text": "The Atlas launch moved again, to 16 December.", "target": "projects/atlas", "as": "fact" }
```

## The arguments

| Argument | What it does | Example |
|---|---|---|
| `text` | What you're telling the wiki, in your own words. Up to 50,000 characters. | `"The Atlas launch moved again."` |
| `target` | The article it's about, or for an instruction, a whole collection | `"projects/atlas"` |
| `as` | `fact` to put it on the record, or `proposal` to ask for a change. Leave it out and Cerebro sorts it. | `"fact"` |
| `kind` | The kind of change, with `as: proposal`. `instruct` when left out. | `"archive"` |

| Kind | Asks for |
|---|---|
| `instruct` | An instruction for the article's agents, such as "Lead with the launch date." |
| `admit`, `archive`, `merge`, `split` | A change to which articles exist |
| `fact.retract`, `record.retract` | Taking back a fact or a record |
| `expire` | Marking a document out of date. People only. |

## Let Cerebro sort it

Leave out `as`, and the decision model sorts your words sentence by sentence. Each sentence can become a fact, a proposal, both, or neither. The four replies you can get:

**A fact.** A sentence that states something goes on the record, as above.

**A proposal.** A sentence that asks for a change becomes one. Yours is accepted at once:

```text
$ cerebro tools tell --json '{"text": "Lead with the launch date.", "target": "projects/atlas"}'
{
  "record": "r_9d55f272bb0d",
  "proposals": [
    "p_9176da63f966"
  ],
  "notes": [
    "Sorted r_9d55f272bb0d sentence by sentence: no sentence to facts, 1 sentence to proposals, no sentence handed back.",
    "Filed and accepted p_9176da63f966. projects/atlas carries the instruction for its writers from their next run."
  ]
}
```

**Both.** One tell can state something and ask for something:

```text
$ cerebro tools tell --json '{"text": "The security review finished on 3 December. Say who owns each step.", "target": "projects/atlas"}'
{
  "record": "r_9b8922533a25",
  "facts": [
    "fact_k35jb6fak7gn3x2etn"
  ],
  "proposals": [
    "p_3dcac100c835"
  ],
  "notes": [
    "Sorted r_9b8922533a25 sentence by sentence: 1 sentence to facts, 1 sentence to proposals, no sentence handed back.",
    "Filed and accepted p_3dcac100c835. projects/atlas carries the instruction for its writers from their next run."
  ]
}
```

**A request for more.** When Cerebro can't tell what a sentence is for, it keeps nothing, hands the words back, and says which arguments settle it:

```text
$ cerebro tools tell --json '{"text": "Something about the launch, maybe.", "target": "projects/atlas"}'
{
  "returned": [
    "Something about the launch, maybe."
  ],
  "notes": [
    "Kept nothing: no sentence of it could be filed.",
    "Didn't know what to do with \"Something about the launch, maybe.\": send it again with as: fact to put it on the record, or as: proposal with a kind to ask for a change, and a target to name the article it is about."
  ]
}
```

Without a decision model, as when its key is missing, every tell without `as` comes back this way. Sorting costs a small model call, counted as extraction.

## Take it back

`retract` takes back a record or a fact by its id. Taking back a record takes its facts and its waiting proposals with it:

```text
$ cerebro tools retract --json '{"id": "r_f80dbd52d80e", "reason": "the date did not move after all"}'
{
  "id": "r_f80dbd52d80e",
  "withdrew": [
    "r_f80dbd52d80e",
    "fact_hz1htqj82h33thwx0e"
  ],
  "kind": "record.retract",
  "proposal": "p_b7c02c4f70ec"
}
```

The words stay kept, marked as taken back. The article's agents drop the fact on their next run. Take back an instruction the same way, by the record that gave it.

## Who may tell what

| Who | How they reach the wiki | Facts | Proposals | Takes back |
|---|---|---|---|---|
| A caller: you, or any MCP client | `cerebro tools`, `cerebro mcp`, or a token | Yes | Any kind, accepted as sent | Anything |
| An app | Its own code, through the `Workspace` handle | Yes | Any kind but `instruct`, waiting for a caller | Only what it told |

An app that tries something it may not do gets one line that says why, and nothing is kept.

## What happens to what you ask

What you ask is accepted as you send it. An instruction lands at once. An admit, archive, merge, split or link goes to the collection's curator, which carries it out on its next run. Here you ask to archive Atlas:

```text
tell { "text": "Atlas is cancelled, archive it.", "target": "projects/atlas" }

{
  "record": "r_862664d98583",
  "proposals": [
    "p_4823559b1ff9"
  ],
  "notes": [
    "Sorted r_862664d98583 sentence by sentence: no sentence to facts, 1 sentence to proposals, no sentence handed back.",
    "Accepted p_4823559b1ff9, archive of projects/atlas: its curator carries it out on its next run."
  ]
}
```

`cerebro serve --once` runs one pass now. `cerebro review` and `cerebro status` list what is accepted and not carried out yet. To take one back before its curator acts, `retract` its `p_` id.

To fix a key or a link, say it in plain words with kind `article.link`, and the curator works out the change:

```text
tell { "text": "Alder's domain is alder.example, not alder.com.", "target": "things/alder", "kind": "article.link" }
```

Every change needs a target. A sentence that asks for a change to another article is handed back, and its facts are kept:

```text
Handed back the change in "Archive Birch too.": it asks for a change to another article, so tell it again with that article as the target.
```

An act into a collection with no curator is refused, since nothing would carry it out.

## Good to know

- **Give a `target` when you can.** A fact with no target still goes on the record, and links to the articles it names.
- **An unknown target is refused,** as `tell: target projects/borealis is no article: find it first, or leave target out`. Find the article first with `find`.
- **One request waits once.** Asking again for a change that already waits is refused, and names the waiting proposal.
- **Long documents belong in a source,** such as a file under `local/`, not in a tell.
