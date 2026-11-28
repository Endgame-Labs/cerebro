Date: 2026-10-28
Protocol: Delta-Lon Transformation and Expression
Strain: E. coli K-12 Delta-Lon
Reagent: Kanamycin 50 mg/mL Stock — Lot K09
Author: Marco Bellini

# Failed Run Write-Up: E. coli K-12 Delta-Lon Low Transformation Yield (PLT-2026-1028-B)

First independent bench transformation by Marco Bellini using E. coli K-12 Delta-Lon (Box B3, Rack 2, -80 C) and pET28a-LbCas12a plasmid following Delta-Lon Transformation and Expression v1.3.

Followed a general textbook 90-second heat shock at 42 C instead of 45 seconds, recovered at 37 C for 60 minutes, and plated on 50 ug/mL kanamycin plates prepared from Kanamycin 50 mg/mL Stock — Lot K09.

Result: severe failure across all 3 replicates — only 12, 14, and 11 colonies appeared after 24 hours (expected >400 colonies), and downstream test elution yields collapsed to 42, 58, and 49 ng/uL.

Dr. Priya Nair reviewed the run and confirmed two compounding failure modes for E. coli K-12 Delta-Lon: the lon protease deletion makes the strain sharply sensitive to heat shock longer than 45 seconds at 42 C, and 50 ug/mL kanamycin stresses recovering transformants. She drafted v1.4 to fix both parameters.
