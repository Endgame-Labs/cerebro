---
evidence:
  - facts: {}
  - articles: [this.linked]
budget: { tokens_per_run: 30000, tool_calls: 16 }
heartbeat: [change]
---

# Instructions

Write one focused claim ledger for the editor, in about 180 words. Cover only the
claim named by this article's title, not the whole ferry reporting file. Lead with
the latest supported position and who says it, then the evidence and its limits,
and finish with the precise open question for the next phone call. Say each thing
once. Use linked source dossiers for roles and attribution. Retrieve more facts only
when a material question cannot be answered from the supplied evidence.

Preserve the exact scope of a correction, including any assertion left standing.
Different measurements stay different, and a blank cost stays unknown. A promised
action is not completed. Distinguish a source's assertion from a checked finding.
Every sentence cites a fact. Link a source id such as sources/dana-vale as ../sources/dana-vale.md.

Use override to set verification_state from this claim's evidence: disputed when
sources still disagree, unresolved when a material answer requires missing evidence,
and corrected when a correction resolves the claim. Use corrected only when the correction resolves every material part of the claim. Keep the linked source ids.

Name each speaker in full in every attributed sentence so it stands on its own.

Cite the motivating facts on every call question, just as on a factual sentence.
