# Changelog

What changed in each release, newest first.

## 0.1.2

- Salesforce email is cleaned the way Gmail's is, so quoted replies, forwarded headers and signatures no longer become facts. A forward with nothing written above it is kept whole, since it may be the only copy of a customer's words.
- Salesforce tasks logged from an email are cleaned the same way.
- A sentence written under a short sign-off such as "Thanks!" is kept.
- Grounding keeps a short label over a list, such as "Ours:", when the facts back what it heads.
- Key names in an admit or a link are read as your sources write them, whatever their letter case or spacing.
- A curator carrying out your yes is recorded as its own act, citing your decision.
- Over MCP, a find or a facts query that matches nothing says why. The waiting note shows only on replies that succeed, and it counts every waiting proposal.
- A run's warnings are kept for what you must act on. A source's note is said once a pass, and the safety edits made to an article are one count on its line.
- A first run numbers its discovery rounds, and guided init says the full run can take an hour or more on a busy workspace.
- The gtm template writes an account's stakeholders as sentences with working links, and its agents look up facts in the query tool's own shape.

## 0.1.1

- The README's Try it is four lines: clone, install, and a guided `cerebro init` that sets up the rest.
- The README says what an update before 1.0 can do to a workspace, and what carries over.
- The test suite passes on a tagged release.

## 0.1.0, first release

- Cerebro builds a wiki from your sources, such as notes, mail, calls and tickets, and keeps it current in a workspace of plain files.
- Agents you write in markdown write the articles, and every sentence cites the fact behind it.
- Claude, Cursor or any MCP client can read the wiki and tell it what changed.
