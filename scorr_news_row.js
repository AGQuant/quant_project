/* scorr_news_row.js — cc#1129 · ONE news row, used by /m/digest and /m/home.
 *
 * The card asks for one shared component and not two copies, and that is the whole point of this
 * file: WHAT MOVED on the digest and LIVE NEWS on home were drifting apart. cc#1120 gave home a
 * category tag; the digest rows never got it and were still a bullet dot plus a wrapped headline
 * in a plain box. Two places rendering the same kind of thing two ways is how they keep diverging,
 * so the markup lives here and both pages call it.
 *
 * It renders MARKUP ONLY and owns no data. Every field comes from the caller's payload — the
 * component never fetches, never sorts and never decides what a row means. In particular it does
 * NOT touch the polished_news ordering, which sorts on polish time (POLISH_LANDING_RULE).
 *
 * TIME-AGO IS COMPUTED FROM polished_news.published_time, the polish time, which is the column
 * digest_v3._news already selects and the only one either page has ever carried. It is not
 * published_at and must not become it: POLISH_LANDING_RULE is that a story lands when it is
 * polished, so an "18m ago" beside a headline means 18 minutes since it landed on the site. A row
 * with no timestamp shows no time rather than a guess.
 *
 * The tag vocabulary is cc#1120's, EXTENDED not rewritten, per the card's do_not_touch.
 */
(function () {
  'use strict';
  if (window.ScorrNewsRow) return;

  /* cc#1259 · THE VOCABULARY, rule 29631: DOM, GLB, AI-ED, IPO. Domestic was IND and AI Editorial
     was AI; both are renamed here and nowhere else, which is the point of this map existing.
     'Stock Views' is KEPT as VIEW even though the rule names only four. It is a real category in
     polished_news, and dropping it would not remove a tag — it would fall through and print the
     full words "Stock Views" in an 8.5px meta line, which is worse than the short form the founder
     did not ask about. Flagged rather than silently decided.
     THE FALLBACK IS NOW EMPTY, NOT THE RAW LABEL. cc#1120 let an unmapped category print itself,
     on the reasoning that an unmapped category is still a fact. Rule 29631 scope 4 overrules that:
     an uncategorised item shows NO tag rather than a wrong one, so an unrecognised value returns
     '' and the row simply carries no tag. */
  var CAT_SHORT = {
    'AI Editorial': 'AI-ED', 'Domestic': 'DOM', 'Global': 'GLB',
    'IPO': 'IPO', 'Stock Views': 'VIEW'
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function catShort(c) { return CAT_SHORT[c] || ''; }

  /* Sentiment vocabulary in polished_news is inconsistent by design — it is passed through raw
     from the source and normalised once, here, so the two pages cannot normalise it differently.
     Anything unrecognised is NEUTRAL, never guessed into a direction. */
  function sentiClass(s) {
    var t = String(s || '').toLowerCase();
    if (/bull|positive/.test(t)) return 'up';
    if (/bear|negative/.test(t)) return 'dn';
    /* /caut/ and not /caution/: the live vocabulary is "Cautious", which does NOT contain the
       string "caution" — it ends -ious, not -ion. The longer pattern silently sent every cautious
       story to the neutral grey. Caught by rendering the real polished_news values rather than
       the ones I assumed were in there. */
    if (/caut/.test(t)) return 'ct';
    return 'nt';
  }

  /* Coarse on purpose. A headline that landed 40 minutes ago and one that landed 44 are the same
     story at a glance, and a ticking minute count on a morning read invites re-reading a number
     that carries no decision. Anything past a week shows nothing rather than "52w ago", which is
     not a thing anyone says about a headline. */
  function ago(iso, nowMs) {
    if (!iso) return '';
    var t = Date.parse(iso);
    if (isNaN(t)) return '';
    var m = Math.floor(((nowMs == null ? Date.now() : nowMs) - t) / 60000);
    if (m < 0) return '';                 /* a clock skew is not a story from the future */
    if (m < 1) return 'now';
    if (m < 60) return m + 'm ago';
    var h = Math.floor(m / 60);
    if (h < 24) return h + 'h ago';
    var d = Math.floor(h / 24);
    return d <= 7 ? d + 'd ago' : '';
  }

  /* opts.category  — the row's category when the payload does not carry one per row (the digest
   *                  queries one category at a time, so the caller knows it and the row does not).
   * opts.chevron   — false to drop the affordance where a row does not open anything.
   * opts.onclick   — inline handler string; omitted entirely when absent, so a row that leads
   *                  nowhere does not pretend to be tappable.
   * opts.now       — injectable clock, so the time-ago is testable without waiting for a minute.
   */
  function render(n, opts) {
    /* cc#1259 · render() INJECTS THE STYLESHEET TOO, and finding out why it did not is most of
       this card. Only list() called injectCss(), but /m/digest builds its news decks by calling
       render() per row — so the digest has been drawing this component's MARKUP with none of its
       CSS since cc#1129, silently, falling back to whatever page styles happened to match.
       That is why the first pass of this card changed nothing on the digest: the rules were
       correct and were never on the page. A component that ships its own stylesheet has to attach
       it on every path that renders, not on the one path that happened to be written first.
       injectCss() is id-guarded, so calling it per row costs one getElementById and nothing else. */
    injectCss();
    n = n || {}; opts = opts || {};
    var cat = n.category || opts.category || '';
    var t = ago(n.published || n.published_time, opts.now);
    var tap = opts.onclick ? ' onclick="' + esc(opts.onclick) + '"' : '';
    var meta = [];
    /* cc#1259: an unmapped category now yields '', so the tag span is not emitted at all —
       an empty bordered pill would be a wrong tag drawn in the shape of a right one. */
    var shortCat = catShort(cat);
    if (shortCat) meta.push('<span class="nrtag">' + esc(shortCat) + '</span>');
    if (t) meta.push('<span class="nrago">' + esc(t) + '</span>');
    /* cc#1370: the raw third-party source/wire-agency name is DROPPED, universal across every
       news-card surface (founder screenshot feedback 28-Aug) — this row keeps only the category
       tag and the polish-time age. .nrsrc had no dedicated rule (it inherited .nrmeta span's
       styling), so the branch removal here is the whole of the change. */
    return '<div class="nrow' + (opts.onclick ? ' tap' : '') + '"' + tap + '>'
      + '<span class="nrdot ' + sentiClass(n.sentiment) + '"></span>'
      + '<div class="nrbody">'
      +   '<div class="nrhl">' + esc(n.headline || '') + '</div>'
      +   (meta.length ? '<div class="nrmeta">' + meta.join('<i>·</i>') + '</div>' : '')
      + '</div>'
      + (opts.chevron === false ? '' : '<span class="nrchev">›</span>')
      + '</div>';
  }

  /* The stylesheet ships WITH the component rather than being copied into each page, for the same
     reason the markup does: two copies drift. Injected once, and it only defines .nrow and its
     children, so it cannot reach anything either page already owns. Colours are read from the
     page's own tokens with a GOLD NIGHT fallback, so the row inherits whatever theme it lands in
     instead of pinning one. */
  var CSS = ''
    /* cc#1130 founder ruling: BORDERLESS. No outer box on a news row, on either screen — the
       hairline between rows is the only border it may carry. `border:0` is declared FIRST and the
       divider after, deliberately: on /m/home the same row is a <button>, and setting only one
       side let the user-agent default (2px outset) stand on the other three, which is the
       rectangle the founder photographed. Written this way the element type cannot reintroduce
       chrome nobody designed. */
    + '.nrow{display:flex;align-items:flex-start;gap:9px;padding:9px 0;border:0;'
    +   'border-top:1px solid var(--edge,var(--line,rgba(148,166,210,.14)))}'
    + '.nrow:first-child{border-top:0}'
    + '.nrow.tap{cursor:pointer}'
    + '.nrdot{width:7px;height:7px;border-radius:50%;flex:none;margin-top:5px}'
    + '.nrdot.up{background:var(--volt,#C8F542)}'
    + '.nrdot.dn{background:var(--heat,#FF4D6D)}'
    + '.nrdot.ct{background:var(--amber,#FF9F45)}'
    + '.nrdot.nt{background:var(--muted,#8A97B0)}'
    + '.nrbody{flex:1;min-width:0}'
    /* two lines then ellipsis: a third line of headline pushes the meta out of the glance */
    + '.nrhl{font-size:12.5px;line-height:1.35;color:var(--chalk,var(--txt,#EAF0FA));'
    +   'display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}'
    /* cc#1259 rule 29631: the tag and the age sit RIGHT-aligned and italic, in the one accent.
       ACCENT PICKED: --amber. It is already the tag colour so nothing regresses, it is a real
       token defined on every surface that renders this row, and the hex fallback keeps it
       correct on a page that has not loaded the token sheet. No per-category colours - the
       card puts those out of scope, and one accent is what makes the line read as one thing. */
    + '.nrmeta{display:flex;align-items:center;justify-content:flex-end;flex-wrap:wrap;'
    +   'gap:0 6px;margin-top:4px;font-style:italic;'
    +   'font-family:\'JetBrains Mono\',ui-monospace,monospace;font-size:8.5px;letter-spacing:.08em;'
    +   'color:var(--amber,#FF9F45)}'
    /* the separator stays upright: a skewed middot reads as a smudge at 8.5px */
    + '.nrmeta i{font-style:normal;opacity:.5}'
    + '.nrtag{font-weight:800;color:var(--amber,#FF9F45);border:1px solid var(--amber,#FF9F45);'
    +   'border-radius:3px;padding:1px 5px;line-height:1.4}'
    + '.nrchev{flex:none;align-self:center;color:var(--muted,#8A97B0);font-size:15px;line-height:1}';

  function injectCss() {
    if (document.getElementById('scorr-newsrow-css')) return;
    var s = document.createElement('style');
    s.id = 'scorr-newsrow-css';
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  /* list() is what both pages actually call: it injects the stylesheet once and maps the rows,
     returning the caller's own empty-state string when there is nothing — so neither page invents
     a placeholder headline to fill the box. */
  function list(items, opts) {
    injectCss();
    opts = opts || {};
    var rows = (items || []).slice(0, opts.limit == null ? 6 : opts.limit);
    if (!rows.length) return opts.empty == null ? '' : opts.empty;
    return rows.map(function (n) { return render(n, opts); }).join('');
  }

  window.ScorrNewsRow = { render: render, list: list, ago: ago,
                          catShort: catShort, sentiClass: sentiClass, injectCss: injectCss };
})();
