---
authority: { admit: true, archive: false, merge: false, split: false }   # admits on its own; an archive or a merge waits for you in cerebro review
evidence:
  - query: { from: facts, window: last_365_days }   # what the visit notes, calls, and labs say
  - query: { from: patients }                      # the patients kept already
heartbeat: [change, daily]
---

# Instructions

Keep one article for each animal the clinic is treating. A patient is an animal a
clinician examined or treated, or one a lab result or an owner's call is about.

Find the patients by name first. Ask each files source that answers it for `notes`,
with each set to patient and with set to owner: every row is one Patient: line as
the notes write it, with its owners. Admit each patient by that name exactly, such
as Rex Gomez, since the name is the animal's name and its owner's family name, and
it is the name that ties each note and lab result to the article. Give each admit
fields: owners, the owners' names as the row gives them, as a list. Give a
one-line reason that names the visits or calls behind it.

The clinic's own staff, the owners, other clinics, pharmacies, and labs get no
article. Neither does a memo about hours, equipment, or staffing, a call that names
no patient, such as a wrong number or an adoption question, or an animal a caller
mentions that the clinic has never seen. Leave those out.

When a patient is already kept, admit nothing for it.
