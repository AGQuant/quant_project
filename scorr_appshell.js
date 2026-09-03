/* scorr_appshell.js — cc#1112
   ═══════════════════════════════════════════════════════════════════════════════════════════
   The app shell's own behaviour, for the three pages that load scorr_appshell.css and nothing
   else: /m/digest, /m/v10 and /m/gvm2. It does two things, and both exist because those pages
   were missing chrome every other /m/* screen already has.

   1 · THE WORDMARK MENU. main.py's auth_gate injects a floating Logout pill and a theme pill
       into every authenticated page. mobile_endpoints.py hides them on the /m/* templates and
       puts Log out behind a tap on the wordmark (cc#909). These three pages load a different
       stylesheet pair, so they got the pills and no menu — the founder photographed both
       sitting on top of the header's own state chip. The CSS now hides the pills, so this file
       has to supply the replacement in the same push. scorr_card_common.js could not be reused:
       none of the three loads it, and its build() needs a `.head h1` the app shell does not have.

       THE THEME ROW IS DELIBERATELY ABSENT. All three pages force data-theme="goldnight" as the
       first statement in <body> and keep the stored theme without applying it, for the Sprint 2
       lock. A "switch to light" row would read the flag, flip it, reload, and change nothing —
       a control that lies. So the menu carries Log out only, and it will carry the theme row on
       the day the lock lifts, not before.

   2 · THE BACK CONTROL. The shell had no way back: the wordmark goes to /m/home and the bottom
       nav does not track history. It appears ONLY when there is somewhere to go back to — a
       same-origin referrer, or real in-app history — and otherwise stays hidden rather than
       rendering a chevron that dumps you out of the app or does nothing.

   Everything here is wrapped so a failure cannot take the screen with it: a header is chrome,
   and chrome must never break the page under it. */
(function () {
  'use strict';

  function sameOriginRef() {
    try {
      if (!document.referrer) return false;
      return new URL(document.referrer).origin === location.origin;
    } catch (e) { return false; }
  }

  /* cc#1119 · THE THEME ROW ARRIVES, AND ONLY TWO THEMES DO WITH IT.
     Founder approved option (a): GOLD NIGHT and DARK AQUA are selectable now; WHITE-GOLDEN
     (goldday) stays hidden. That is not caution for its own sake — scorr_themes.css declares 13
     tokens per theme and the rest of the palette falls through from scorr_theme_r5.css, whose
     values are DARK. Both themes offered here are dark, so every fall-through lands somewhere
     consistent with them. goldday is a LIGHT field, so the same fall-through would put
     dark-theme text colours on a near-white surface. The gate is the fall-through, not taste.
     The stored key is never rewritten by omission: a phone that already holds 'goldday' keeps
     holding it, and simply renders goldnight until the day that theme ships. */
  /* cc#1636 (founder 02-Sep): all FOUR token sets are real choices now, and this menu no longer
     writes storage or the body attribute itself - it calls window.SCORR_THEME.set (the resolver in
     pwa_endpoints APP_THEME_RESOLVE_JS), the one path that validates, stores, applies and announces
     scorr:theme. The local fallback mirrors that contract for a page whose head lost the resolver. */
  var THEMES = [
    { k: 'goldnight', label: 'Gold Night', ic: '◆' },
    { k: 'dark',      label: 'Dark',       ic: '◇' },
    { k: 'aquawhite', label: 'Aqua White', ic: '○' },
    { k: 'goldday',   label: 'Gold Day',   ic: '◈' },
    { k: 'blush',     label: 'Blush',      ic: '❀' },   /* cc#1637 */
    { k: 'rosenight', label: 'Rose Night', ic: '✿' },
    { k: 'rosewall',  label: 'Rose Wall',  ic: '❉' }
  ];
  var THEME_OK = { goldnight: 1, dark: 1, aquawhite: 1, goldday: 1, blush: 1, rosenight: 1, rosewall: 1 };

  function storedTheme() {
    var T = window.SCORR_THEME; if (T && T.get) return T.get();
    try { return localStorage.getItem('scorr_theme'); } catch (e) { return null; }
  }
  function applyTheme(k) {
    var T = window.SCORR_THEME; if (T && T.set) { T.set(k); return; }
    try { localStorage.setItem('scorr_theme', k); } catch (e) {}
    document.body.setAttribute('data-theme', THEME_OK[k] ? k : 'goldnight');
  }

  function buildMenu(wm) {
    var menu = document.createElement('div');
    menu.className = 'as-menu';
    /* the theme row sits ABOVE Log out: it is the thing you come back to, and Log out is the
       thing you press once. A separator keeps a mis-tap off the exit. */
    menu.innerHTML = THEMES.map(function (t) {
        return '<button type="button" class="as-th" data-k="' + t.k + '">'
          + '<span class="ic">' + t.ic + '</span>' + t.label
          + '<span class="as-tick" aria-hidden="true"></span></button>';
      }).join('')
      + '<div class="as-sep"></div>'
      + '<button type="button" id="as-lo"><span class="ic">⏏</span>Log out</button>';
    wm.classList.add('as-has-menu');
    wm.appendChild(menu);

    function markCurrent() {
      var cur = THEME_OK[storedTheme()] ? storedTheme() : 'goldnight';
      Array.prototype.forEach.call(menu.querySelectorAll('.as-th'), function (b) {
        b.classList.toggle('on', b.getAttribute('data-k') === cur);
      });
    }
    markCurrent();
    Array.prototype.forEach.call(menu.querySelectorAll('.as-th'), function (b) {
      b.onclick = function () {
        var k = b.getAttribute('data-k');
        /* APPLIED IN PLACE, no reload, through the one set path (applyTheme -> SCORR_THEME.set). The themes are pure CSS custom properties on <body>, so
           swapping the attribute repaints everything that reads them. A reload here would cost a
           full refetch of the page's data to change a colour, and on the digest that is the whole
           morning read. */
        applyTheme(k);
        markCurrent();
        menu.classList.remove('open');
      };
    });

    /* the wordmark is an <a href="/m/home">. It stays a real link for anyone who wants home —
       the menu opens on tap and the navigation is suppressed only while it does. */
    wm.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      menu.classList.toggle('open');
    });
    menu.addEventListener('click', function (e) { e.stopPropagation(); });
    document.addEventListener('click', function () { menu.classList.remove('open'); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') menu.classList.remove('open');
    });

    var lo = menu.querySelector('#as-lo');
    if (lo) lo.onclick = function () { location.href = '/logout'; };
  }

  function buildBack(hdr, wm) {
    var canBack = sameOriginRef() || history.length > 1;
    if (!canBack) return;                    /* no dead chevron */
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'as-back on';
    b.setAttribute('aria-label', 'Back');
    b.innerHTML = '‹';
    b.onclick = function () {
      /* history.back() is right when the previous entry is ours. With only a same-origin
         referrer and no usable history — opened fresh from a notification or a bookmark —
         /m/home is the honest destination rather than leaving the app. */
      if (history.length > 1) history.back();
      else location.href = '/m/home';
    };
    hdr.insertBefore(b, wm);
  }

  function build() {
    try {
      var hdr = document.querySelector('.as-hdr');
      if (!hdr) return;
      var wm = hdr.querySelector('.as-wm');
      if (!wm) return;
      if (!wm.querySelector('.as-menu')) buildMenu(wm);
      if (!hdr.querySelector('.as-back')) buildBack(hdr, wm);
    } catch (e) { /* chrome must never break the screen under it */ }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', build);
  } else {
    build();
  }
})();
