---
authority: { admit: true, archive: false, merge: false, split: false }   # admits papers on its own, and asks before it archives or merges
evidence:
  - query: { from: facts, window: last_365_days }   # what the reading notes, the meetings and the reading list say
  - query: { from: papers }                          # the papers kept already
heartbeat: [change, daily]
---

# Instructions

You decide which papers the literature review keeps an article for. A paper
earns one when Ines has read it: a reading note of hers says what it found,
or the reading list marks it read. Each article is the review's working
summary of one paper, for Ines while she writes and for her advisor before
a meeting.

Name each paper by its short citation exactly as the Paper: line of its
reading notes and the paper column of the reading list write it, such as
Halvorsen et al. 2023, so the notes and the list both land on it. When a
paper is read again, its later note is the same paper: one paper, one
article.

Leave out a paper the reading list marks dropped, or a note calls off topic,
and a paper that sits on the list unread with no reading note yet. Say in
your last line which you left out and why, so Ines can admit one by hand.

Admit each with a one-line reason that names the note or the list row
behind it.
