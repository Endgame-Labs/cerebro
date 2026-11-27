# Why Cerebro works this way

This page explains the ideas behind Cerebro, so you can predict what it does and write agents that work with it. It says why, not how the code is built.

## Your sources are kept as they came

Everything Cerebro knows starts as a record: a note, a message, a call, a ticket, or something you tell it. A record is kept whole and never edited. If a source changes a record, Cerebro keeps each version, and marks the facts that only the old one gave as superseded.

This means you can always trace a sentence in the wiki back to the words it came from. Nothing in the wiki rests on something Cerebro cannot show you.

## Facts come from records

Factoid, Cerebro's facts layer, reads each record and pulls out the facts it states. Each fact cites the record it came from. Cerebro makes no fact of its own, and no code decides what a field of an article says.

A tell works the same way. When you tell the wiki something, Cerebro keeps your words as a record and turns them into facts, proposals, or neither. It never edits an article directly.

## Curators decide which articles exist

Each collection, such as `projects`, can have a curator: an agent that reads the facts and decides which articles belong. It admits an article, archives one, merges two, or splits one in two. It names each article with keys, such as a name or a domain, and Cerebro uses those keys to tie new records and facts to the right article.

A curator does not need a step for each record. Once an article has the right keys, Cerebro binds what matches on every pass.

## Writers write from evidence

An article's agents write its text from the evidence their file asks for: facts, records, other articles. Every sentence cites the fact behind it. An editor scores each sentence from 0 to 1 by what its facts mean, in any words, language or style, and the writer sees the scores once while it can still revise.

Cerebro never cuts or rewrites what a writer wrote. A sentence that still scores under the floor, `min_grounding`, lands marked with a footnote that says it is not fully backed by the facts it cites. You read the whole article, and see which claims are weak.

Cerebro hides nothing from a writer. When a fact is old, retired, or from another article, Cerebro labels it and lets the writer judge. What makes a fact relevant is what it says, not where it came from.

## A model decides, code guards

A curator or a writer is a model following a prompt. It makes the judgment calls: what matters, what to write, which article a record is about. Code does not second-guess those calls, because a better model gets them right more often, and a code rule stays wrong.

Code holds the lines that must never break, whatever a model says:

- Cerebro runs a workspace only from a folder you trust.
- Secrets are masked in everything stored, logged, returned, and sent to a model.
- Every model call is checked against your spending cap before it starts. Once the cap is met, nothing new starts.
- Records are never changed. Only a run writes an article.
- Every HTTP request needs a live token.
- A curator does not undo your decision until something new about it arrives.
- Text from a source reaches a model as data, never as instructions.
- The fields that name, place, and bind an article are Cerebro's. Whatever a model sets there, Cerebro's value wins.

When an agent makes a poor call, change what it is shown or told in its file, then run it again.

## An article is free text

An article is whatever its agents wrote, with any headings they like. You read it as one piece of text. Cerebro imposes no outline on it.

An article can be built by several agents, each writing a part. Only the part whose evidence changed runs again. You still read the result as one article.

## You decide, and curators carry it out

A caller is you, at the command line, or any client that reaches the wiki with a live token. What a caller asks for is decided as it is sent. An admit, archive, merge, split, or link goes to the collection's curator, which carries it out on its next run. An instruction to writers lands at once.

A token proves who may call. It does not limit what that caller may decide.

Every decision about an article is a proposal: who asked, what for, who decided, and when. A curator's `authority:` line says what it may do on its own. Anything else waits for you in `cerebro review`.

A few rules follow:

- **Your decision stands.** A curator does not undo it, yes or no, until a new fact or record about it arrives.
- **A second proposal adds information.** It is filed beside the first, not refused. Only the same ask from the same caller, while it still waits, is refused.
- **A command means the same everywhere.** It does the same thing and gives the same answer at the command line, over MCP, and over the JSON API.

## Your files decide

Cerebro's bundled prompts teach an agent how to curate or write. Your agent file says what your workspace wants. Where the two differ, your file wins.

Cerebro adds a setting only when someone would want to change it. If a default is always right, it is simply how Cerebro works.

## Templates and apps sit on top

Cerebro knows records, facts, articles, agents, and apps. It knows nothing about sales, research, or code. A template, such as `gtm` for a sales team, is a ready-made set of agents built on the same parts you use.

An app, such as Notes, adds tools and kinds of record through the same interface any app uses. It is not part of the core. If an app needs something new, it becomes a capability every app can use.
