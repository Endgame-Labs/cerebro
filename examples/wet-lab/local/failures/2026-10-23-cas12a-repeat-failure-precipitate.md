Date: 2026-10-23
Protocol: CRISPR-Cas12a Lateral Flow Assay
Reagent: Buffer X — Lot 4B
Reagent: LbCas12a Enzyme Stock — Lot E20
Author: Soren Lindqvist

# Failed Run Write-Up: Cas12a Repeat Failure and Buffer X Lot 4B Precipitate (PLT-2026-1023-A)

Repeated CRISPR-Cas12a Lateral Flow Assay v2.2 on plate PLT-2026-1023-A using a fresh unopened tube of LbCas12a Enzyme Stock — Lot E20 paired with Buffer X — Lot 4B, testing both 37 C and 42 C side by side.

All 3 positive replicates with Buffer X — Lot 4B failed again at both temperatures: no visible test line, erratic Ct values of 34.8, 36.4, and 38.6, and yield readouts of 22, 17, and 12 ng/uL. When tipping the Buffer X — Lot 4B bottle to pipette the last replicate, Soren Lindqvist spotted a dense layer of white crystalline precipitate at the bottom of the bottle stored at 4 C.

Soren ran a quick side control using 100 uL of leftover Buffer X — Lot 3A at 42 C with the same fresh LbCas12a Enzyme Stock — Lot E20: the positive band returned immediately, proving LbCas12a Lot E20 is NOT degraded. However, the 42 C no-template control showed a faint false-positive cleavage band, which did not appear at 37 C.

Root cause identified: Buffer X — Lot 4B precipitates Tris-MgCl2 salts when stored at 4 C, depleting free Mg2+ required for Cas12a catalytic activity, while 42 C incubation introduces non-specific reporter cleavage in no-template controls.
