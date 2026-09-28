/* Idea ux-header-width-rules: one rule for every header (Winds Aloft, Fly
 * with Kate!, Admin, My Account) - words next to the icons whenever the
 * whole row fits on one line, icon-only when it doesn't, and the shared
 * ~992px breakpoint (Bootstrap's own navbar-expand-lg) takes it down to
 * the hamburger menu when even the icons don't fit.
 *
 * The icon/word split can't be one fixed pixel width, because accounts
 * show a different number of buttons (a CFI's Fly with Kate! header needs
 * far less room than an admin's) - see the QA finding this fixes. Instead,
 * for each header, this shows the word next to every icon, checks whether
 * doing that made any single button grow taller (i.e. its label wrapped
 * onto a second line), and keeps the words only when nothing wrapped.
 * That naturally adapts to however many buttons THIS account has.
 *
 * Below the hamburger breakpoint the collapsed menu is a single column
 * (see the max-width:991.98px rule in style.css), so this only has
 * anything to check once the navbar-toggler is hidden.
 */
(function () {
  'use strict';

  function hostFor(label) {
    return label.closest('.nav-link, .dropdown-toggle, .btn') || label.parentElement;
  }

  function updateNav(nav) {
    var toggler = nav.querySelector('.navbar-toggler');
    if (toggler && toggler.offsetParent !== null) {
      // Collapsed (hamburger) mode: the menu is a full-width single
      // column, so labels always fit - nothing to measure.
      nav.classList.add('hdr-words');
      return;
    }
    var labels = nav.querySelectorAll('.nav-label');
    if (!labels.length) { nav.classList.add('hdr-words'); return; }

    nav.classList.remove('hdr-words');
    var before = [];
    var i, host;
    for (i = 0; i < labels.length; i++) {
      host = hostFor(labels[i]);
      before.push(host.getBoundingClientRect().height);
    }

    nav.classList.add('hdr-words');
    var fits = true;
    for (i = 0; i < labels.length; i++) {
      host = hostFor(labels[i]);
      if (host.getBoundingClientRect().height > before[i] + 1) { fits = false; break; }
    }
    nav.classList.toggle('hdr-words', fits);
  }

  function scan() {
    var navs = document.querySelectorAll('nav.navbar');
    for (var i = 0; i < navs.length; i++) updateNav(navs[i]);
  }

  var t = null;
  function scheduleScan() {
    if (t) clearTimeout(t);
    t = setTimeout(scan, 80);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scan);
  } else {
    scan();
  }
  window.addEventListener('resize', scheduleScan);
  window.addEventListener('load', scan); // icon font finishing can change widths slightly
})();
