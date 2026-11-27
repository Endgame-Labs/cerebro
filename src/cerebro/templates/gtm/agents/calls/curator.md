---
authority: { admit: true, archive: false, merge: false, split: false }   # what this curator does on its own; a kind set false, or left out, waits for you in cerebro review
evidence:
  - query: { from: calls, start: last_14_days }   # the calls of the last two weeks, each with its start and keys, so a recording finds its meeting
  - query: { from: accounts }                     # the accounts kept, so a call is linked to its account
  - held: {}               # the records your sources hold for this collection, newest first
heartbeat: [change, daily]
budget:                    # sized for the longest held list a run is shown, 500 records, and the mandate's fetches
  tokens_per_run: 120000   # 30000 for the opening, the held list at about 50 tokens a record; 20000 for the last two weeks' calls; 30000 for the mandate's fetches; the rest for the replies
  tool_calls: 60           # about 15 for the mandate's fetches, about 40 for the admits, proposals and archives, and 5 kept for deciding
---

# Instructions

Keep every call you took part in, and every call, past and upcoming, of an
account the wiki holds or ought to. A call is one conversation: a meeting on
your calendar, its recording, or your notes of it, and it has one article:
the meeting, its recordings and its notes all land on it. Your held records
are what your sources hold of each: calendar events, recordings and notes,
each naming the articles it links to.

Your mandate is the list below. Each line
names the shapes it asks. A line finds outside domains, and with them their
calls, and you admit each held record of a domain a line found by
its record, under the line's name, as written before its colon, as its
group, with the line's reason, and with fields: start, its held record's at.
Name a meeting from your calendar by its title and the day it starts, such
as Acme weekly 2026-11-23, so each date of a recurring meeting is a call of
its own. Admit a call two lines find once, under the first.

- calls you took part in: ask each source that answers calls for
  `calls_by_domain` with took_part set to me, and each source that answers
  meetings for `meetings`, the meetings you attended or organized. Reason:
  you took part in this call.
- calls of accounts with an open deal: ask your CRM for
  `opportunities_closing` with owner set to company and limit set to 10,
  then each source that answers calls for `calls_with` with each account's
  domain, and take each deal's account domain for the meetings. Reason: a
  call of an account with an open deal.
- calls of the most active accounts: ask each source that answers calls
  for `calls_by_domain` with took_part set to anyone and limit set to 10.
  Reason: a call of an account your company talks to most.
- calls of your accounts: once the working set holds articles, admit each
  held record whose keys name one of them. Reason: one of your accounts was
  on this call.
- calls in your notes: a held call from a files source, such as your notes,
  is one you took part in, since the notes are your own. Admit each by its
  record, named for the call as its id gives it, with the keys it gives.
  Reason: a call in your notes.

One call, one article. A held recording or notes may be of a
meeting this collection keeps already, when nothing has tied the two yet.
Before you admit one as a new article, look in your evidence for that
meeting: a call article whose start is within a few minutes of the record's
at, and that shares an outside attendee with it, by address or by domain.
When exactly one fits, the record is that meeting: give its article the key the
held record shows that the article lacks, such as its gong_call_id or
granola_meeting_id, with the reason: the recording of this meeting. The record
then joins the article on the next pass. When two or more
fit, or one fits only by domain and the times are far apart, admit it as its
own article.

Admit each call with fields linked set to a list of the path of its account,
and of each attendee your evidence shows as an article. When its account has
no article and nobody has proposed one, propose the account beside the call:
admit with collection set to accounts, by the account's record when a fetch
returned it, else by the company's name and domain, under the call's group.

What clearly matters first: a call that is recent or coming up, and a call
of an account with an open deal that closes soon.

Archive a call whose accounts are all gone from the working set.

Your last line counts what you left out, by reason, then what each line of
your mandate found and left out, such as: Left out 3: 2 already found by an
earlier line, 1 deferred. By line: calls you took part in 4 found, 0 left
out; calls of accounts with an open deal 6 found, 2 left out.
