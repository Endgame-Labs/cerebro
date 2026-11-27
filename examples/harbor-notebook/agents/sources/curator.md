---
authority: { admit: false, archive: false, merge: false, split: false }
evidence:
  - files: [local/interviews/dana-vale.md, local/interviews/tomas-vale.md, local/interviews/leena-orr.md, local/interviews/imani-reed.md, local/interviews/oskar-chen.md, local/interviews/bea-north.md]
  - query: { from: facts, limit: 150 }
budget: { tokens_per_run: 30000, tool_calls: 16 }
heartbeat: [change]
---

# Instructions

The interview files name the sources before extraction. Give each admit the source
key from its Source header, copied exactly, so the interview binds to its dossier.

Maintain source dossiers for the editor, one per interviewed person with substantive ferry evidence.
Use each person's full name as the article name and key. Dana Vale and Tomas Vale
are different people with different roles. Propose at most six dossiers.
Kitchen notices, lost property, and people named only incidentally do not need dossiers.
