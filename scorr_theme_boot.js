/* scorr_theme_boot.js — cc#1203 push 3. The web theme, chosen before the first pixel.
 *
 * WHY THIS FILE IS INLINED AND NOT LINKED. It has one hard requirement: html[data-theme] must
 * carry the right value BEFORE the browser paints anything. A <script src> — even in <head>, even
 * without defer — is a network round trip, and the two <script defer> siblings in _MOBILE_HEAD run
 * only after the document is parsed. Either way the page paints dark first and then snaps to
 * light, which is the flash this card exists to remove.
 *
 * So main.py reads this file at import and embeds its contents directly in the injected head. The
 * file still exists as the one place the logic is edited and reviewed; it is simply delivered
 * inline rather than fetched. That is also why it must stay small and dependency-free — it runs
 * before anything else on the page.
 *
 * WHAT IT DOES, in order:
 *   1. reads localStorage.scorr_theme, defaulting to dark
 *   2. stamps html[data-theme] synchronously
 *   3. exposes window.ScorrTheme.set(name) for the shared pill (push 4)
 *
 * EVERY STEP IS WRAPPED. localStorage throws outright in a browser configured to block site data,
 * and this script runs before the page has any error handling of its own — an exception here
 * would take the whole document with it. A reader with cookies disabled gets the default theme,
 * not a blank page.
 *
 * APP SURFACES DO NOT GET THIS. /m/* and /preview/* are the app's black-and-gold contract and are
 * pinned dark by _MOBILE_APP_DARK (cc#1064); main.py injects this on web paths only.
 */
(function () {
  "use strict";
  var KEY = "scorr_theme";
  var DEFAULT = "dark";
  /* The set the app will accept. An unknown value in storage — a typo, an older build's name,
     something a person set by hand — falls back to the default rather than being stamped through,
     because html[data-theme="navy"] matches no block and would render an unstyled page. */
  var ALLOWED = { dark: 1, light: 1 };

  function read() {
    try {
      var v = window.localStorage.getItem(KEY);
      return (v && ALLOWED[v]) ? v : DEFAULT;
    } catch (e) {
      return DEFAULT;      /* storage blocked — the default is still a working page */
    }
  }

  function apply(name) {
    var v = ALLOWED[name] ? name : DEFAULT;
    try {
      document.documentElement.setAttribute("data-theme", v);
    } catch (e) { /* nothing sensible to do this early; never throw out of the boot */ }
    return v;
  }

  var current = apply(read());

  window.ScorrTheme = {
    get: function () { return current; },
    /* Persist AND apply. The write is tried first but a failure does not stop the apply: a reader
       with storage blocked should still be able to switch theme for the life of the page, even
       though it will not survive a reload. */
    set: function (name) {
      current = apply(name);
      try { window.localStorage.setItem(KEY, current); } catch (e) {}
      /* Announced so a page that paints its own canvas or chart can repaint on the new tokens —
         the digest charts in push 5 need exactly this. */
      try {
        window.dispatchEvent(new CustomEvent("scorr:theme", { detail: { theme: current } }));
      } catch (e) {}
      return current;
    },
    toggle: function () { return this.set(current === "dark" ? "light" : "dark"); }
  };
})();
