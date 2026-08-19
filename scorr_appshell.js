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

  function buildMenu(wm) {
    var menu = document.createElement('div');
    menu.className = 'as-menu';
    menu.innerHTML = '<button type="button" id="as-lo"><span class="ic">⏏</span>Log out</button>';
    wm.classList.add('as-has-menu');
    wm.appendChild(menu);

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
