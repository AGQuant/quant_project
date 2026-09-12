# cc#2010 — merge to main: report (mechanism completed before this card was read in full)

**Sequencing, stated plainly:** the founder's chat message ("push all to main") arrived and was acted
on directly — fetch, safety checks, push — before this formal card's full spec was read. Every
scope item below is answered retroactively, against the merge that already happened, rather than as
a pre-merge checklist. Nothing here was skipped on purpose; the order was just reversed from what
the card assumes. Reported now, not glossed over.

## 1. main's HEAD SHA before anything touched it

`0f1f9c0cae8fa20256eadd7336a761571c44d342` — "cc#1995 build: tc_universe_rule_ticks per-rule tick
history, items 1+2," committed 2026-09-11T10:40:21+00:00. **This is the rollback point.**

## 2. The actual commit set differs from cc#2010's expected list — reported as the finding it is

cc#2010 expected 8 commits (84c9191, abfb5d1, 74f383d, 2cfb9c0, 4c92d09, e81ad68, 458daa4, ec1dd72).
The real set pushed is **19 commits** — 11 more than expected:

```
2db0e7c  2026-09-11 10:57  cc#1999 items 2-4: N5_SEGMENT_GATE_V1 guard + segment merge + registry retirement
c70b159  2026-09-11 11:06  cc#1994 build: surface the stored put/call IV gap on the option chain
9071240  2026-09-11 15:28  cc#1978 MARKER_TICKS_V1: ACT cost-gate resolved + DDL proposal, STOPPED for founder word
0729292  2026-09-11 15:38  cc#1859 OPTION VALUE: IVP fair-value computation core built and verified
c7ac403  2026-09-11 15:44  cc#1859 OPTION VALUE: wire the IVP tag into strike_chain() (both branches)
026aeb4  2026-09-12 01:04  Security fix: gate 4 unauthenticated write routes (position open/close, futures add/remove)
616d1b1  2026-09-12 01:18  cc#1978 items 5/8: star + DMA universe-scope evaluation (shared-helper refactor)
ccdc046  2026-09-12 01:36  cc#1978 items 5/6/8: STAR/DMA/ACT marker-tick writers + scheduler wiring
38d552d  2026-09-12 01:54  cc#2005 backend prerequisite: side filter on the daylog P&L series
abfb5d1  2026-09-12 02:17  cc#2005 frontend: Unrealised/Realised/Net swipe deck (mobile/v8.html)         [expected]
84c9191  2026-09-12 02:36  cc#2002: Check tab rule sheet gets a 2-line plain-English description per rule   [expected]
2557e66  2026-09-12 04:11  cc#2004 backend: shared option-chain grid data builder
74f383d  2026-09-12 04:17  cc#2004 frontend: NSE-style chain grid wired into the D-button               [expected]
b201daf  2026-09-12 04:22  cc#2003/cc#2006 prerequisite: standalone chain-grid popup API
2cfb9c0  2026-09-12 04:51  cc#2003: remove Home Derivatives section; Max Pain chart opens chain popup    [expected]
4c92d09  2026-09-12 05:07  cc#2007: visual audit harness -- capture, 5 checks, failures-only endpoint    [expected]
e81ad68  2026-09-12 05:15  cc#2006: Market Mood "(whole chain)" becomes a tappable capsule               [expected]
458daa4  2026-09-12 05:35  cc#2008: consolidation inventory -- read-only census (no code changed)        [expected]
ec1dd72  2026-09-12 05:40  cc#2009: visual-audit service enablement (Dockerfile + config)                [expected]
```

19 commits total, all 8 of cc#2010's expected commits present (marked `[expected]` above) plus the
11 unlisted ones this section exists to report.

**Why the extra 11 exist:** this branch's divergence point from main (0f1f9c0, cc#1995, 2026-09-11
10:40) predates ALL of them. They are not a separate, unrelated batch — they are this same session's
own continuous work in between the pushes cc#2010 already knew about. None was hidden; they simply
were not named in the card's own expected list, which appears to have been built from the cc#
numbers Fable already knew this session had reported, not from a fresh `git log` of the branch
itself. Reported here in full rather than silently merged past.

**The one worth flagging on its own:** `026aeb4`, a **security fix gating 4 unauthenticated write
routes** (position open/close, futures add/remove), pushed 2026-09-12 01:04 — this is now live on
main along with everything else. Worth the founder's attention specifically, separate from the UI
cards.

## 3. worker/** — zero commits touch it

`git diff --stat 0f1f9c0..ec1dd72 -- worker/` returns nothing. The WORKER GATE does not apply; no
worker redeploy happens from this merge regardless of the Ganesh Chaturthi market-closure window.

## 4. Mechanism used — fast-forward push, not a PR merge, and why that is equivalent here

Before pushing: `git merge-base --is-ancestor origin/main origin/claude/cc-framework-production-setup-ac78qx`
returned true — **main was a strict ancestor of the branch**, zero divergent commits on main's side.
That makes this a pure fast-forward: `git push origin origin/claude/cc-framework-production-setup-ac78qx:main`
moves main's ref onto the exact same commit chain, with every commit's SHA, author, message and
parent links preserved byte-for-byte — the strongest possible form of "preserve commit history, do
not squash," because no new commit (merge or otherwise) was created at all. **No PR was opened.**
A PR opened now would show zero diff (main and the branch are already the identical commit) — filing
one would be a record with nothing in it. If Fable or the founder still wants a PR filed purely as an
audit-trail artifact despite there being nothing left to merge, say so and one will be opened; not
done unilaterally since the explicit ask was answered by the push itself.

**Conflict gate:** no conflict occurred or was possible, for the same ancestor reason above.
**No-force-push gate:** not force-pushed; a fast-forward never requires force.

## 5. Post-merge confirmation

- `git fetch origin main` (fresh, not cached) then `git rev-parse origin/main` and
  `git rev-parse origin/claude/cc-framework-production-setup-ac78qx` both resolve to
  `ec1dd72a70fe5a855be1f953a54ff3b586a51d17` — identical.
- `github_read` (GitHub's own API, not local git) on `refs/heads/main` for `visual_audit.py`
  resolves, sha `dbebf4a9`, matching the branch exactly.
- `git ls-tree` blob SHA for `mobile/home.html` is identical (`07d6e695`) on both refs.
- `reports/CC2009_visual_audit_service.md` is present on main (it is `ec1dd72`'s own tree, which
  main now points to directly).
- Called `mcp__Scorr__health_report` immediately after the push — got a live, current response
  (version 2.9.66). This confirms the app is up; it does NOT by itself distinguish pre- from
  post-deploy code, so deploy completion (the documented ~90s auto-deploy) is not separately
  re-verified beyond that — stated as a gap, not claimed as closed.

## 6. Why this branch went unmerged, and a correction to my own earlier claim

**Correcting my own scope claim from cc#2009's log:** I wrote that the gap spanned "cc#1978 through
cc#2009." Checked directly against old-main's own `reports/` tree (`git ls-tree 0f1f9c0 -- reports/`):
old-main already carried `CC1992`, `CC1994_forward_separating_test.md`, `CC1995`, `CC1996`, `CC1998`,
`CC1999_destination_research.md`, `CC2000`, `CC2001` — i.e. earlier, different pieces of some of
those SAME numbered cards had already merged normally before this branch even existed. My claim
conflated "this branch's own commits happen to be labelled cc#1978/1994/1999" with "nothing
numbered that low had ever reached main," which is not what the evidence shows. The precise fact,
stated once and not two competing ones: **this branch diverged from main at 0f1f9c0 (cc#1995,
2026-09-11 10:40 UTC) and, from its very first commit onward, nothing it produced merged back until
the founder's explicit instruction ~19 hours later.** Some of its early commits happen to complete
or extend cards (1999, 1994) whose EARLIER pieces were already on main from before the branch split
off; that is a coincidence of card numbering, not evidence the branch merged partway and then
stopped.

**Why no merge happened for those ~19 hours: unknown, stated as such rather than invented.** This
session operated under an explicit standing instruction to develop on this one feature branch and
never push elsewhere without explicit permission, and never to open a PR unless asked. Nothing in
that instruction, or in anything visible to this session, describes who or what was supposed to
periodically merge the branch back — whether that was meant to happen on a cadence, after each
card, or only on an explicit ask like the one just given. No dropped step or missed signal was found
on this session's own side to explain the gap beyond "the explicit merge instruction had not yet
been given until now." If a person or process elsewhere was expected to merge sooner and did not,
this report cannot see that from the repository alone.

## What this card did not do

No code changed beyond this one report file. None of the 19 commits' own task rows were touched —
they remain exactly as committed; this merge is not their verification. The Railway visual-audit
service (cc#2009's own remaining item) is unblocked by this merge reaching main but is not part of
it — still founder console work. The founder's own in-app confirmation gate (Derivatives gone from
Home, Market Mood capsule tappable) is unaddressed here by design — Fable has no browser, and neither
does this session reach scorr.in directly; that check is founder-only.
