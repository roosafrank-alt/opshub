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

/* QA ux-account-menu-behind-hamburger: on a phone/tablet/narrow window, the
 * account menu (_account_menu.html) and the hamburger's collapsed #nav panel
 * sit in the same top-right corner and can both be open at once, with the
 * panel stacking over the dropdown so only a sliver of "My Account"/"Log
 * Out" peeks out. Only one of the two should ever be open: tapping the
 * account icon while #nav is expanded (or still in the middle of its own
 * open animation from a just-tapped ☰ - #nav only gets the "show" class
 * once that finishes, so a check for "show" alone misses a quick
 * ☰-then-account double-tap) now closes #nav, so the dropdown opens fully
 * in front, right under the icon. The other direction (hamburger closes
 * the account menu) already works on its own, because Bootstrap's dropdown
 * auto-closes on an outside click.
 * Shared by both headers (Winds Aloft and Fly with Kate!), since both use
 * the same #nav collapse id and the same account-menu toggle id prefix.
 * #nav is a fixed-top-edge floating panel (see the "Mobile view drop downs"
 * rule in style.css: position:absolute, top:100%) whose CLOSE is a height
 * animation - its top edge doesn't move, so mid-transition it still covers
 * the account dropdown sitting right under the same corner. Closing it
 * instantly (no transition) here avoids that half-open overlap; a normal
 * hamburger tap still gets its usual animated open/close.
 */
(function () {
  'use strict';
  function closeNavInstantly(navEl, Collapse) {
    navEl.style.transitionDuration = '0s';
    navEl.addEventListener('hidden.bs.collapse', function restore() {
      navEl.style.transitionDuration = '';
      navEl.removeEventListener('hidden.bs.collapse', restore);
    });
    Collapse.getOrCreateInstance(navEl).hide();
  }

  document.addEventListener('show.bs.dropdown', function (ev) {
    var toggle = ev.target;
    if (!toggle || !toggle.id || toggle.id.indexOf('user-menu-toggle-') !== 0) return;
    var navEl = document.getElementById('nav');
    var Collapse = window.bootstrap && window.bootstrap.Collapse;
    if (!navEl || !Collapse) return;
    if (navEl.classList.contains('show')) {
      closeNavInstantly(navEl, Collapse);
    } else if (navEl.classList.contains('collapsing')) {
      // Still animating open (or closed) from a ☰ tap a moment ago: Bootstrap
      // only adds "show" once that finishes, so wait for it to settle, then
      // make sure it ends up closed rather than popping open afterward.
      var settle = function () {
        navEl.removeEventListener('shown.bs.collapse', settle);
        navEl.removeEventListener('hidden.bs.collapse', settle);
        if (navEl.classList.contains('show')) closeNavInstantly(navEl, Collapse);
      };
      navEl.addEventListener('shown.bs.collapse', settle);
      navEl.addEventListener('hidden.bs.collapse', settle);
    }
  });
})();
