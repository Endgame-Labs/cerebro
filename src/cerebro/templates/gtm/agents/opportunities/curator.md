---
authority: { admit: false, archive: false, merge: false, split: false }   # what this curator does on its own; a kind set false, or left out, waits for you in cerebro review
evidence:
  - query: { from: accounts }   # the accounts kept, so a deal is linked to its account
  - query: { from: opportunities }   # the deals kept, each with the account it links to
  - held: {}               # the records your sources hold for this collection, newest first
heartbeat: [change, daily]
budget:                    # sized for the longest held list a run is shown, 500 records, and the mandate's fetches
  tokens_per_run: 100000   # 30000 for the opening, the held list at about 50 tokens a record; 30000 for the mandate's fetches; the rest for the replies
  tool_calls: 150          # about 15 for the mandate's own fetches and 3 for each account of the working set, about 30 accounts; about 40 for the admits, proposals and archives, and 5 kept for deciding
---

# Instructions

Keep every deal you own or work, and every deal of an account the wiki
holds or ought to. Your mandate is the list below. Each line names the
shapes it asks, and each row it lists is a deal. For each line, admit its
deals into this collection: an admit for each deal the line lists, by its
name with the keys its row gives, under the line's name, as written before
its colon, as its group, with the line's reason, and link the deal to its
account. Never give admit a key that is not a row's. Admit a deal two lines
find once, under the first.

The questions fetch answers of deals: opportunities_i_own,
with owner me or reports, opportunities_closing, in the next days with owner
me, reports or company, and, by an account's id or exact name,
opportunities_of_account, its open deals, opportunities_past_due, its open
deals past their close date, and opportunities_closed, its deals closed in
the last days, all in Salesforce. Each gives one row for each deal, with
its stage, amount, close date, owner, and its account's id, name and domain.
A row with articles under bound is on
those articles already. When that deal links to another account than the one
whose salesforce_id is the row's account id, the deal has moved: link it to
that account, and take the old one off with unlinked. Admit any other row by
its keys as given, with fields: its stage, amount, close_date, owner and
is_closed as the row gives them, and linked set to a list of its account's
path. When its account has no article and nobody has proposed one,
propose the account beside the deal: admit with collection set to accounts,
by the account's record when a fetch returned it, else by its name and domain
as the row gives them, under the deal's group.

What clearly matters first: a deal that closes soon, and a deal you or your
reports own.

- deals you own: ask your CRM for `opportunities_i_own`. Reason: an open
  deal you own.
- deals your reports own: ask your CRM for `opportunities_i_own` with owner
  set to reports. Reason: an open deal one of your reports owns.
- deals closing in the next 90 days: ask your CRM for
  `opportunities_closing` with owner set to company. Reason: an open deal
  closing in the next 90 days.
- deals of the accounts you own: ask your CRM for `accounts_i_own`, then
  `opportunities_of_account` with each account's id. Reason: an open deal
  of an account you own.
- deals of your accounts: ask your CRM for `opportunities_of_account` with
  the salesforce_id of each account article a query from accounts lists.
  Reason: a deal of an account in the working set.
- late deals of your accounts: ask your CRM for `opportunities_past_due`
  with the salesforce_id of each account article a query from accounts
  lists. Reason: an open deal of an account in the working set, past its
  close date.
- closed deals of your accounts: ask your CRM for `opportunities_closed`
  with days set to 365 and the salesforce_id of each account article a
  query from accounts lists. Reason: a deal of an account in the working
  set, closed in the last year.

Propose an archive of a deal whose accounts are all gone from the working
set.
