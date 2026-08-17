# SCORR APP REPORT — APP_QA_R3
**Date:** 17-Aug-2026 · **Author:** Fable · **Status:** DRAFT — supersedes R2 (chain: R1, R2 kept)
**Single change vs R2:** WS6 corrected per founder (10:35 17-Aug). §§0–5 otherwise as R2; findings as R1.

---

## WS6 — HOME THEME SWITCHER (corrected)
**Two schemes exactly, first release:**
- **DARK — the current theme.** Black/petrol Telemetry R5, byte-for-byte unchanged. The switcher's default.
- **LIGHT — WHITE-GOLDEN (new).** Warm white field, gold accents. Draft tokens: field `#FAF7F0` · panel `#FFFFFF` · panel-hi `#F3EEE3` · edge `#E5DFD2` · ink `#1A1A1E` (body + titles) · muted `#8A8578` · gold `#A67C00` for text-weight accents (eyebrows, links, active nav — dark enough to pass contrast on white) · gold-fill `#D4AF37` for rails, meters, glows · **win `#1F9D61` · loss `#D93B4F`** — P&L stays green/red in BOTH schemes, money truth never re-skins.
**Resolves the BGW DECIDE:** white-golden IS the phase-2 golden direction (spec 23980 tokens invert onto light ground); no separate third theme.
**Implementation:** one CSS-variable token layer + `data-theme="dark|light"` on the page root; no markup forks; preference persists per device. Home first; other pages inherit only after founder approves home in both schemes.
**Design-ref first:** `design_refs/scorr_home_themes_R1.html` — the home hero + one data card rendered in BOTH schemes with the switch control visible. Fable ships this next; founder comment round precedes any build.
**Verify:** zero layout shift between schemes; identical values both schemes; P&L hues identical; toggle survives reload; contrast ≥ WCAG AA for body text in LIGHT.

## Sequencing (unchanged from R2)
WS1 → WS2 → WS3 → WS6 → WS4 (per-page refs) → phase-2 items closed (BGW resolved into WS6).

## FOUNDER OBSERVATIONS *(open — R4 = EXECUTABLE candidate)*
- F1 thumb verdict (Digest tile: old page or new?): ______
- Theme switch placement (header toggle vs settings row): ______
- Nav "Intel" tab fate + web Index-Intel tile (carried from R1): ______
- WS4 order changes / anything missed: ______

## Changelog
- R3 17-Aug: WS6 = DARK(current) + LIGHT(white-golden); BGW DECIDE resolved into WS6.
- R2 17-Aug: modus operandi; WS6 added (superseded reading); WS4 replica pass.
- R1 17-Aug: initial audit, F1–F8, WS1–WS5.
