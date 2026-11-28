Component: Number printer
Date: 2026-11-02

# R02: A value disappears on paper

Mira Vale found Number printer renders 1.0000000001 as 1 under the shipped eight-digit policy. The numeric result remains 1.0000000001 internally. R02 is an open output precision regression for copy-and-paste users. This is unrelated to Parser losing signed zero.
