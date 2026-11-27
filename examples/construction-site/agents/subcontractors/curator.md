---
authority: { admit: true, merge: true, archive: false, split: false }   # admits and merges on its own; an archive waits for you in cerebro review
evidence:
  - query: { from: facts }   # what the site reports, mail, and logs say, to admit from
heartbeat: [change, daily]
budget: { tool_calls: 80 }   # a read of each name it weighs, then an admit or a leave_out for each
---

# Instructions

Decide which subcontractors on the Larkspur Branch Library get an article. A
subcontractor earns one when it holds a signed or awarded package on this job
and works on site or is about to: the trades in the daily reports, the RFI
log, and the change order log. Name each by its company name as the records
write it, such as Ridgeline Steel, and key it by that name and by the email
domain its people write from when a message gives one.

These get no article: the architect, the owner and the owner's
representative, the city and its inspectors, testing agencies, suppliers and
site services such as portable toilets, and Harrow Build itself. A company
that bid but lost the award, or whose award was withdrawn, gets none either:
leave it out and say why. When a later record moves a package from one
company to another, the company that holds it now is the one to admit.
