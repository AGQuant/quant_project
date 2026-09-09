# FINKHOZ brand mark

cc#1876 (09-Sep-2026): the founder asked to save the transparent FINKHOZ
logo for future use. It was not in the repo, not in `app_config`, and not
in any DB table (checked `information_schema` for logo/asset/brand/media/
image columns and `app_config` for logo/brand/finkhoz/data:image keys —
0 rows either way). `hr_report_pdf.py` is deliberately white-label with
zero branding (cc#652) so it carried no copy either. The only source on
hand was page 1 of `FINKHOZ_Corporate_Profile_Deck_v3_ForContentTeam.pdf`,
which is actually a ZIP of page JPEGs, not a real PDF.

## Provenance

The mark was alpha-keyed off the deck cover JPEG (a white mark on an
orange gradient) to a 62×68 transparent PNG, then **rebuilt as straight-
edge polygons** fitted to that mask. Pixel IoU of the rebuilt vector
against the extracted mask is **0.961** (48 px missed, 7 px extra, of
1,402 mark pixels).

**This is a redraw, not the original vector.** If the design team
supplies the original vector, **it should replace these files.**

## Files

| File | Fill |
|---|---|
| `finkhoz_mark.svg` | `currentColor` — inherits the surrounding text/CSS colour |
| `finkhoz_mark_white.svg` | literal `#FFFFFF` |
| `finkhoz_mark_orange.svg` | literal `#FF5A3C` |

All three share the same geometry and `viewBox="0 0 62 68"`.

## Scope

Static assets only — nothing wires these into `main.py` or any route,
and no page's `NAV` array changes. Use them by reading the file content
directly (inline SVG) wherever a FINKHOZ mark is needed; there is no
HTTP route serving them and none is planned here.
