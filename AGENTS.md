# Working on Cerebro

Read this before you plan or build a change, whether you are a person or a coding agent. Check your work against it before you open a pull request.

Cerebro builds a wiki from a person's sources. Sources list records, Factoid turns records into facts, curators decide which articles exist, and context agents write articles from evidence. The Python framework is in this folder.

The reasons behind these rules are in [docs/philosophy.md](docs/philosophy.md). Read it before you design anything.

## The five rules that get broken most

Habit pulls toward the wrong side of every one of these.

### 1. The core is domain agnostic

Cerebro is a framework. Sales (the `gtm` template) is one use of it. Nothing in the engine may assume accounts, people, deals, a CRM, or any other domain's idea.

- **The test:** would this code path work unchanged for a research lab, a law practice or a codebase?
- **Where a domain rule goes:** a template (`src/cerebro/templates/`), the connector that issues a source's own key, or the workspace's files. Never the engine.
- **No collection is first or main.** Order comes from the data, such as what hangs off what, never from a name.
- **This covers comments, docstrings, examples and engine tests.** Use neutral names there (`things`, `parts`, a `parent:` header). Sales words belong in template tests.

### 2. A model decides. Code guards.

The usual way to handle a judgment is a few hundred lines of deterministic code: a classifier, a router, a validator, a wrapper with flags. Here it is usually wrong. One model question, or a line in an agent's own prompt, holds the same complexity for a fraction of a cent, and it bends when the content changes where code breaks.

- **Before you write a helper that classifies, routes, filters, ranks, validates free text or picks among options, ask:** can the agent's prompt decide this, or one question to the judge?
- **A flag that restates what a prompt decides is wrong.** The prompt alone determines what an agent does with what it is given.
- **Do not build schemas over free text.** Prompting changes the shape of content, and a rigid structure breaks.
- **Do not guard what cannot happen, and do not guard what is not ours to guard.**
- **When a model decides, measure it.** A prompt line is not a control until a run on real models shows the difference. Keep a labelled set and run it again when wording changes.

**What code must always enforce.** These are the things that must never happen whatever a model says. Keep them in code, tested, and do not soften them:

- Cerebro runs a workspace only from a folder its owner trusts.
- Secrets are never printed or stored in the clear, and are masked in everything stored, logged, returned and sent to a model.
- Every model call is checked against a spending cap before it starts. The cap is a soft limit: once it is met nothing new starts, and a run may end over it by the calls already under way. A few cents over is fine. Dollars over is not. Do not build holds, reservations or estimates to make the cap exact.
- Inputs are append-only. Only a run writes an article.
- Every HTTP request needs a live token.
- A curator does not undo what a caller decided until something new about it arrives. A caller's instruction outranks an agent's. A caller is anyone who sends a command, at the command line or over MCP or the JSON API with a live token. A curator, an agent and an app are not callers.
- Text from a source reaches a model as data, never as instructions.
- Fields the system relies on are protected: whatever a model sets there, the system's value always overwrites it. They are the fields that address and name an article, place it in the working set, bind records to it, and record what compile and the sources say of it. Every other field belongs to the agent's own data domain, and the agent is free to set or rewrite it.

The line between the two lists: code stops harm, a model chooses what is best.

### 3. An article is free text

There is no "sections" construct. An article is whatever its agents wrote, with any headings they like. A caller gets it as one text field.

- Nothing a caller, a person at the command line, an agent file, a prompt or a doc sees may treat an article as a list of sections, or say the word.
- Inside the engine, a composed article is stored as one piece per agent, so only a stale agent runs again. That is a private fact about storage. Call it a part, or content. Never expose it as a shape someone must follow.
- Code may cut text at headings to find a passage for search. It must cope with text that has no heading at all.

### 4. An app is not the core

Notes is an app. It lives under `apps/notes/` and uses the framework like any app would. It is not a primitive of Cerebro, and neither is any template.

- The engine knows the kinds an app declares. It does not know what a note is. No app's word in engine code.
- If a design starts from "Cerebro has notes" or "Cerebro has accounts", stop. Cerebro has records, facts, articles, agents and apps.
- A need an app has becomes a general capability any app can use, or it stays in the app.

### 5. Cerebro mints no facts

Every fact comes out of Factoid, from a record. A tell is a record that triage turns into facts and proposals, or neither, each citing it. It writes no field. Front matter has two writers: a curator when it admits an article, and the article's agents. No code decides what a field says.

## Words

| Say | Not |
|---|---|
| workspace (the folder) | brain |
| wiki (what agents write) | brain |
| article | page |
| part, content | section |
| Cerebro (the program doing something) | the brain |

Tests guard these in what a person sees (`tests/test_workspace_words.py`, `tests/test_output_words.py`). Add new screens, messages and prompts to them.

## How a caller is treated

- **Nothing is hidden.** Label a thing and let the reader judge. Do not leave it out.
- **Relevance comes from what a fact says,** not from where it came from. Where it came from is shown as that.
- **Replies are small.** Leave out what is empty. Return what changed, and let the caller ask for more by id.

## How we work

- **One change, one job.** A pull request has one subject and says which files it changes.
- **A bug's proof is a test that fails before the fix.** Write a script only where a test cannot reach, such as real models, timing or a real workspace. Keep it in `checks/` if it is worth keeping.
- **A change to what a model is asked, or to what lands, is proven on real models before it lands.** The suite alone is not proof.
- **A large change gets an adversarial review** by someone who did not build it.
- **Never word a prompt to pass a check question.** A line must state a rule the writer needs.
- **Report what you did not verify** as plainly as what you did. Say when a claim is someone else's.
- **Commits and pull requests:** plain language, no attribution lines, no model names. Titles and commit messages carry no ticket ids. Merge, never rewrite history.
- **Ask a maintainer** before you add a construct, a key, a flag or a word a person will see, and before you change what the framework assumes. A bot reviewer does not make design decisions.
- **Ask a maintainer before you change how a workspace is stored.**
- **Prompt, tool-description and template words are yours to get right.** Revise them against `docs/philosophy.md`, proofread them, prove them on real models on both sides of the change, and bring the measured result. A new term a person will see still goes to a maintainer.
- **Bring a maintainer few questions,** in the order that unblocks the most work, each a yes or no with your pick.
- **Write the smallest change that does the job.** It is easy to write more code than the goal needs. Report lines added and removed. A reviewer hunts for what need not exist as well as for what is wrong.

## Good code

Each line is something a reviewer can check.

- **One way to do a thing.** Search before you write.
- **No helper with one caller,** unless its name states a rule. No wrapper that only passes arguments on.
- **No option, flag or parameter that nothing sets.** Nothing "for later".
- **What a change makes unused goes in the same change.**
- **A function fits on one screen. A file has one subject.**
- **A comment states a limit the code cannot show, in one or two lines.** No history, and no account of the change.
- **An error is raised where it is found, with words a person can act on.** No catch and carry on.

## Tests

It is easier to write tests than to judge which tests need to exist. Write fewer.

- **Before you add a test, name the break in `src` it would catch that no existing test catches.** If you cannot, do not write it.
- **The measure is breaks caught, not lines run.** Prove a new test by breaking the code once and seeing it fail.
- **A test guards a behaviour a caller or a person can see, or a hard line from the list under rule 2.** One test per behaviour.
- **Every hard line has a named test that fails when the line is broken.** Keep the list, hard line and test, in `tests/hard_lines.md`.
- **Every command and every MCP tool has one test that it works and one that it refuses.**
- **Every bug fixed gets one test that failed before the fix.**
- **The suite has a budget: 5 minutes on 3 workers.** A change that takes it over, or adds time while it is over, trims before it lands.
- **Two tests that fail on the same breaks are one test.** Keep one.
- **Do not pin a prompt's sentences in a test.** A real-model check measures a prompt. A test may still check a prompt for words the words table bars.
- **A sweep gets its boundary cases, not every offset.** A matrix gets the paths the code really takes, not every combination.
- **Change the test that exists.** Do not stack a `_fix`, `_gaps` or `_recheck` file on an older file.
- **Name a test for the behaviour it guards,** never for the bug or the review that found it.
- **Do not test** the test's own data or helpers, a private name or an internal shape, a sentence another test already pins, a timing on a shared machine, or a case that cannot happen.
- **A test that starts a process or waits real time is costly.** Write one only where the behaviour needs it.
- **Report tests added and removed.** A change that adds many says what each group guards.

## Docs

The docs under `docs/` say how to use Cerebro, not how it is built. They are written for a first-time user who is short of time and has no patience for jargon. `docs/philosophy.md` explains why Cerebro works as it does, not how it is built.

- **Lead with what the reader can do,** then the shortest working example, then the options.
- **No history.** No account of how a thing is built, and no edge case a first-time user will never meet. A limit that will bite a real user gets one line.
- **Paragraphs of 2 to 4 sentences, bullets of 1 to 2.** Use a table or a code block where it helps, and give an example for each point.
- **Plain short words, second person, present tense.** No em dashes and no semicolons in prose. Code terms and command names stay exact.
- **Every command, flag, file and output shown is true of the code today.** Run it, or read it, before you write it. Use the fakes where a real model would be needed.
- **Ask of every line:** is this useful to the reader, or detail that wastes their attention? Cut the second kind.
- **A test may run what the docs show. It never checks what the docs say.** A test may load a settings, source or agent-file block from a page, or run its SQL. It never asserts a sentence, a heading or a number written in words.

## Signs you are drifting

- You are naming a thing after the sales template.
- You are adding a flag, a mode or a registry to choose between two behaviours.
- You are writing a parser or a schema for text a model wrote.
- You are deciding in code something a curator or a writer could be asked.
- You are protecting against a case you have not seen happen.
- You wrote "section".

## Keeping this file

When a maintainer corrects something a contributor should have known, add it here in the same change. Write it as a clear instruction, never as a quote or a story. Keep each rule short. If this file passes 200 lines, cut examples, not rules.
