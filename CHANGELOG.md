# Changelog

What changed in each release, newest first.

## 0.1.3

- Grounding no longer cuts or rewrites what a writer wrote. The decision model scores each sentence from 0 to 1 against the facts it cites, and the writer gets the sentences under the floor back once, with each score and why, while it can still revise. A sentence still under the floor lands with the footnote `[^unsupported]: Not fully backed by the facts it cites.`
- The floor is 0.6. Set `min_grounding` in `settings.yaml` for every agent, or in an agent's front matter for that agent. A heading is read as context and never marked, a mark never lands inside code, and a Chinese or Japanese sentence ends at its full-width stop.
- A run's line counts what grounding marked, as `grounding projects/atlas: marked 1 sentence not fully backed`, and each sentence's score and why stay on the run's row of the `agent-runs` log. `cerebro compile --article` prints that line too, and `cerebro status` counts from each agent's latest run. With no decision model, nothing is scored or marked, and the run says how many sentences it left unscored.
- When you take a fact or a record back, or a source removes the record, Cerebro takes out the lines whose every cited fact was taken back, from text its writer did not rewrite in that run. Text a writer just handed in is never cut, and a take-back by a curator, an agent or an app cuts nothing.
- A writer is told which article it writes, so one whose evidence holds the facts of many articles writes about the right one. It is also told of each bracketed number, such as `[1]`, that cites nothing, and asked to cite by id, take it out, or keep it when it is no citation.
- When a second record states a fact again, the footnote shows each record's words under that record's name, as `a.md "..." · b.md "..."`, so no footnote quotes words its record does not hold. A writer, the judge, `read` over MCP and the JSON API show a fact the same way.
- A writer whose model hands in no text and gives no reason, even when asked again, now fails its run, and a later pass tries it again. A subagent that does this keeps the part it had. An agent that says it has nothing to write still shows `wrote nothing` and is no failure.
- When a run fails, is cut or stops after a writer's first hand-in, that text still lands. When the run failed, a `warning` line says why, in plain words.
- An agent's replies stream, so a long reply that keeps coming finishes. A reply may take 10 minutes to start, as a model that thinks first can be silent, and then times out only after a minute with nothing new.
- A model call no longer leaves an asyncio warning on your terminal.
- `retries` on a model, in `settings.yaml` or in an agent's `model:`, sets how many times a timeout, a rate limit or a server error is tried again. It is 3 by default, and a refused key is never tried again. Each try is checked against your cap before it starts, and each failed try counts in what its run spent.
- A run's `ran` line splits its cost into writing and checks once the checks cost anything, as `$0.0155 (writing $0.0150, checks $0.0005)`. Its row in the `agent-runs` log keeps `input_tokens` and `output_tokens`.
- Through a gateway, only a 401, 402 or 403 stops the pass. A 400 fails that one call, and the pass goes on.
- A failed call to the decision or extraction model, and the line that stops a pass at a gateway, say what to do for the status. A refused key points at the key, or at `providers:` in `settings.yaml` through a gateway. A 402 says payment is due at the provider, a busy or failing provider is named as busy or down, and any other status points at the model id and the request.
- A first run is quicker. Each curator runs once, side by side, from what your sources hold of the last 14 days, and proposes what clearly matters. Later passes fill in the rest, and a curator that fails runs again on a later pass.
- What you approve is written once, from those 14 days, from any older line you tick, and from the older records of its own collection that its keys bind. On the list, an older record reads `not weighed yet: older than 14 days`. When older records still wait once every build has run, a `notice` line says how many, and `cerebro serve` reads them on its next passes.
- The first run no longer asks what you want to see first, and `cerebro serve --see-first` is gone.
- The first run's list counts under an article the records it would bind, as `4 records`. An article one curator proposes in another collection, such as what an item hangs off, is a line you can tick, joined with that collection's own proposal of it. A line you tick again on its own lands on its own.
- `cerebro review --accept-from` decides each file it names on its own, and says which had nothing waiting. It also takes a curator's first list that a later pass filed.
- A curator that carries out an approved admit by the article's path keeps the name you approved, and can bring back an archived article that way.
- `cerebro agents new <collection>/curator` writes a `held: {}` line, so the new curator sees the records your sources hold for its collection.
- On a first run, a curator's `held` line lists every record held for its collection, also one that links only to articles nobody has made or proposed yet.
- A field's fact keeps the value as your source gives it. A value that is another record's key, such as a parent's id, is no longer shown as that article's title and path.
- A row of a files source's csv, jsonl or json file leaves a column with no value, or only spaces, out of its text, so the facts layer no longer reads the next column's name as its value.
- Each `sql` line's rows get ids of their own, so two `sql` lines on one source no longer drop each other's rows. A row's footnote quotes the row, and the same query spaced apart keeps its ids.
- With Factoid 0.1.30, when you edit a record, the sentences you left alone keep their facts.
- Salesforce reads each account, contact, user and deal by its own id once it is admitted, with the tasks, events and emails logged on it. An account's contacts, deals and owner are no longer read with it. So `opportunities.closed_since` goes: delete it if your `sources/salesforce.yaml` sets it, or the source stops with a line that names it.
- A Salesforce source answers two new questions a curator asks with `fetch`: `opportunities_past_due`, an account's open deals past their close date, and `opportunities_closed`, its deals closed in the last `days`, 90 by default.
- A Salesforce record admitted and then deleted before a pass read it is removed on the next pass, and its article archived.
- A `query` line on facts takes a list for `classes` and `kind`, as a `facts` line does. A collection query says how many articles it left out for lacking a field it filters on.
- After a pass is killed, the next pass takes over every lease it held, not only the first, so no agent waits for one to lapse.
- The table in the `AGENTS.md` that `cerebro init` writes into a new workspace renders as a table.
- The gtm template's curators say what clearly matters first. Its calls, opportunities and people curators propose an item's account beside it when nobody has, and the opportunities and people curators follow a deal or a person that moved to another account. The opportunities curator also asks for an account's past-due deals and those closed in the last year, and the people curator for the contacts of each account the wiki keeps.
- The docs say which article fields Cerebro keeps beside a source's own, where a one-article agent's title comes from, how two collections that read each other compile, and who may take a fact back.
- `sql` row ids changed in 0.1.3, and so did the evidence line of a fact a second record restates. After an upgrade, every agent with a `sql` line or such a fact runs once more, on the first pass its cooldown allows. Nothing is doubled or lost.
- A subagent with no `model:` of its own is now charged at the model it runs on, the compilation model in `settings.yaml`, not at its composer's. If its composer names a cheaper model, a pass counts more toward your cap than before, and `cerebro status` and the `agent-runs` log name the model it ran on.
- `repair` goes from `compile:` in `settings.yaml`, and from the gateway's `{call}` values, since grounding rewrites nothing. If your `settings.yaml` sets `compile: { repair: ... }`, delete it, or every command stops on it.
- A prompt you copied into `prompts/` with `cerebro prompts eject` keeps the words it had, so a copy of `rules` or `unwritten` still tells writers a sentence is cut. Delete the copy, run `cerebro prompts eject rules` or `cerebro prompts eject unwritten` for the new words, then make your edits again.
- A gtm workspace made before 0.1.3 keeps its old agent files, and no command updates them. Their curators do not ask for an account's late or closed deals, which Salesforce no longer reads with the account, and their deal and people writers look for an account path in brackets that no fact gives now. From your workspace folder, run `cp -R <clone>/src/cerebro/templates/gtm/agents/. agents/`, where `<clone>` is the folder you cloned Cerebro into. Then redo any edits you made to those files, and delete again any you had removed.

## 0.1.2

- Salesforce email is cleaned the way Gmail's is, so quoted replies, forwarded headers and signatures no longer become facts. In Gmail and Salesforce alike, a forward with nothing written above it is kept whole, since it may be the only copy of a customer's words.
- Salesforce tasks logged from an email are cleaned the same way.
- Links and inline images in email lose their markup, so an address or a number reads as written, and an image line no longer makes a short reply or a forward disappear.
- A sentence written under a short sign-off such as "Thanks!" is kept.
- Grounding keeps a short label over a list, such as "Your side (Dana Wu):", when the facts under it hold its names and figures.
- A run cut short by its budget keeps the fields it set only when some of its text stands, for a composed article's agents as for a writer.
- Key names in an admit or a link are read as your sources write them, whatever their letter case or spacing, and a link decision you made holds under any spelling of its key.
- A curator carrying out your yes is recorded as its own act, citing your decision.
- Over MCP, a find, a facts query, or a log or collection query that matches nothing says why. The waiting note shows only on replies that succeed, and it counts the proposals that wait for a person, as `cerebro status` does.
- A run's warnings are kept for what you must act on. On a terminal its last line names what waits for you, a source's note is said once a pass, and the safety edits made to an article are one count on its line, kept in full in the run's log.
- A pass's plain output leaves out counts of zero, but still says when a source listed from the start holds nothing. Under `cerebro serve`, the daily-cap stop line is said once.
- A first run numbers its discovery rounds, and guided init says the full run can take an hour or more on a busy workspace.
- The gtm template writes an account's stakeholders as sentences with working links, its calls article reads a call's account and attendees whole, and its agents look up facts in the query tool's own shape.
- Three tests that failed now and then are steady.

## 0.1.1

- The README's Try it is four lines: clone, install, and a guided `cerebro init` that sets up the rest.
- The README says what an update before 1.0 can do to a workspace, and what carries over.
- The test suite passes on a tagged release.

## 0.1.0, first release

- Cerebro builds a wiki from your sources, such as notes, mail, calls and tickets, and keeps it current in a workspace of plain files.
- Agents you write in markdown write the articles, and every sentence cites the fact behind it.
- Claude, Cursor or any MCP client can read the wiki and tell it what changed.
