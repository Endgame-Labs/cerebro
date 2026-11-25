---
authority: { admit: false, archive: false, merge: false, split: false }   # what this curator does on its own; a kind set false, or left out, waits for you in cerebro review
evidence:
  - query: { from: accounts }   # the accounts kept, so a deal is linked to its account
  - held: {}               # the records your sources hold for this collection, newest first
heartbeat: [change, daily]
budget:                    # sized for the longest held list a run is shown, 500 records, and the mandate's fetches
  tokens_per_run: 100000   # 30000 for the opening, the held list at about 50 tokens a record; 30000 for the mandate's fetches; the rest for the replies
  tool_calls: 120          # about 15 for the mandate's fetches, about 100 for an admit for each line and a call for each proposal and each archive, and 5 kept for deciding
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
me, reports or company, and opportunities_of_account, by an account's id or
exact name, all in Salesforce. Each gives one row for each open deal, with
its stage, amount, close date, owner, and its account's id, name and domain.
A row with articles under bound is on
those articles already. Admit any other row by its keys as given, with fields:
its stage, amount, close_date and owner as the row gives them, is_closed false,
and linked set to a list of its account's path.

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

Propose an archive of a deal whose accounts are all gone from the working
set.
