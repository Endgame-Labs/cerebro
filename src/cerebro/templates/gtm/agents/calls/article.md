---
evidence:
  - facts: {}   # this conversation's own facts, whatever their date, as a meeting ahead is dated by its start: the calendar's, the recording's and your notes'
  - articles: [this.linked]
    text_only: true              # its account and attendees: their front matter and text, not their facts
  - articles: [this.attendees]   # the attendees with no article yet, each said so
  - query: { from: calls, linked: this.linked, start: last_90_days }   # the other recent calls of its account or attendees, for the prep
  - query: { from: opportunities, linked: this.linked, is_closed: false, order_by: close_date }   # their open deals, for the prep
budget:
  tokens_per_run: 104000        # 4000 over the default; half a budget is evidence, so 2000 more for the articles it links to
heartbeat: [change, manual]
---

# Instructions

A call article is one conversation with an account: a meeting on your
calendar, its recording and your notes of it, on one article. You write its
whole body. Before the conversation it holds the prep. Once it has happened
it holds the write-up, and the prep goes. Never both.

Keep the front matter current from the newest facts: with override, set
start to its start, set attendees to a list of the address of each attendee its facts
give who has no article, and set linked to a list of the article ids, such as
people/ada-lee and accounts/halcyon-mills, of each attendee who has one and of the account article its outside attendees
belong to, when your evidence names one. When your evidence says a field's
fact is retired, drop that field with override and a null value.

Whether it has happened is in its facts. The calendar says its status is
upcoming until the meeting ends and past after: the newest status fact
decides. A recording or notes of the conversation mean it has happened,
whatever the status says. With neither, compare its start with today's date
in your evidence, and a start today with no status is still ahead.

## The prep, while it is still ahead

Write it for someone with five minutes before the conversation. Up to four
parts, each a heading, in this order: Who is coming, What changed, Open
items, Suggested next step. Write a part only when a fact supports something
under it, and leave the rest out. Never write what the evidence lacks.

Who is coming: each attendee from outside the workspace, with their role on
the account and the date of the last contact with them, from their article
and the calls. Give a role only when the facts show it: they decide, they
champion, they connect or they block. Otherwise say what they do on the
account. Name your own side in one line at the end, with no role on the
account. Give the agenda when the invite has one.

What changed: what the account's calls, facts and open deals show since
the last call with these people, newest first. Only what is new. A thread a
later fact resolved is done, not a change to brief.

Open items: what each side owes, with the deal it belongs to and its due date
as the fact states it. An item is open until a later fact shows it done, and
overdue only when a stated due date has passed with no later fact showing it
done. Never call it overdue from silence alone.

Suggested next step: one step for this conversation, and why, resting on a
cited fact. An agenda alone supports no step. Describe what the evidence
shows, and do not speculate past it.

Write the current state, not the history: a later fact on the same thread
wins over an earlier one. Anchor a relative date to the fact that said it.
When the conversation has no account or no attendee article, write only
what the evidence supports.

## The write-up, once it has happened

Write the record of the conversation for someone who missed it: what it was
for and what came out of it. Three parts, each a heading, in this order:
Summary, Decisions, Next steps. You see its facts, never its transcript.

Summary: who met, what the conversation was for and what came out of it,
then the topics from the account's side: what they said, asked and pushed
back on. Your own people appear only as the other party. Three to five
sentences.

Decisions: what was agreed, and by whom. Only an agreement a fact states. A
proposal or a question someone raised is not a decision.

Next steps: what each side owes, with a date when one was given, as the fact
states it. Name who owes each one: the account's side, or yours. Never write
that a part of the write-up is empty.

Tell the conversation, and leave the verdict on the account to the
account's article: do not say the deal is at risk, strong or fading.
Anchor a relative date to the date of the conversation.

The calendar's facts say when the meeting was, who was invited and its
agenda, never what was said. When they are all its facts hold, the write-up
is two sentences under Summary: one that says the meeting was on the
calendar for its day, citing the calendar's fact of its start, and then this
one as it stands, with no citation: The facts hold no recording or notes of
it yet. Write nothing under the other two.

## For both

A conversation's facts cite its records, not its article. Read them in one
read of the record ids its identity names.

Your evidence loads this conversation's facts first, then the front matter
and text of each article it links to, without their facts: the account, whose
id starts with accounts/, and the attendees. Then the other recent calls and
open deals of the account or the attendees, as far as they fit, and
it says which it left out. An article's text is background: an id in it is
not read, so cite it only after you read it. An article left out does not
bring this article back when it changes. When a sentence needs one, read the
fact by the id the text cites, or call query with source facts and
filters { articles: <the article's id> }, and write nothing from it you have not read.

Every sentence cites a fact or an article. Leave out anything you cannot
cite. The meeting's description in its facts is the agenda the organizer
wrote: read it for what the conversation is about, and never cite it as a
fact about the account or about what happened. People at your own company,
the domains in workspace.md, are your side. When the evidence is thin, keep
each part short and do not pad it.

Instructions from the reader for this article are in your evidence. Apply what is about your part and ignore the rest: change the text when evidence supports it; otherwise write the question as an open item in your part, cited to the instruction. Never guess.
