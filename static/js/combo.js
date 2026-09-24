/* OpsHub search pickers.
 *
 * Turns the app's drop-downs of people, planes, parts and projects into the
 * same one-box picker as Schedule Flight's Student field: click or tap the
 * box and the whole list drops down under it; type and the list narrows with
 * every character (matching anywhere, matching letters highlighted). Tap a
 * row, or arrow keys + Enter (Enter picks the top match). Escape or clicking
 * away puts back the current pick.
 *
 * The real <select> stays in the page (hidden) and is what gets submitted
 * and what every other script on the page reads and watches, so nothing on
 * the server or in the page scripts has to change. Picking fires the
 * select's normal "change" event (so onchange="this.form.submit()" filters
 * keep working).
 *
 * Which drop-downs get it:
 *   - any <select data-combo>
 *   - selects that pick a person / plane / part / project (by name or id)
 *   - any other drop-down with 8 or more choices
 * Never: multi-selects, list boxes (size > 1), <select data-no-combo>,
 * selects already hidden on purpose (.visually-hidden) or sitting inside
 * an .input-group.
 */
(function () {
  'use strict';
  var PICKER_NAMES = /^(student_id|cfi_id|asset_id|plane_id|part_id|project_id|laborer_id|user_id)$/;
  var PICKER_IDS = /(project-select|operator-select|student-select|cfi-select|plane-select|asset-select|part-select)$/;
  var MIN_OPTIONS = 8;

  function addStyles() {
    if (document.getElementById('opshub-combo-css')) return;
    var st = document.createElement('style');
    st.id = 'opshub-combo-css';
    st.textContent =
      '.ohc{position:relative}' +
      '.ohc .input-group{flex-wrap:nowrap}' +
      '.ohc-list{position:absolute;left:0;right:0;top:100%;z-index:1060;max-height:280px;overflow-y:auto;display:none;margin-top:2px;min-width:220px;background:#fff;color:#212529;text-align:left}' +
      '.ohc-list.open{display:block}' +
      '.ohc-list .list-group-item{padding:.45rem .75rem;cursor:pointer;font-size:.95rem;font-weight:normal;white-space:normal}' +
      '.ohc-list .list-group-item.ohc-act{background:#e7f1ff;color:#084298}' +
      '.ohc-list .list-group-item.ohc-chosen{font-weight:700}' +
      '.ohc-list .list-group-item.ohc-off{color:#adb5bd;cursor:default}' +
      '.ohc-list .ohc-grp{font-size:.75rem;text-transform:uppercase;letter-spacing:.03em;color:#6c757d;background:#f8f9fa;cursor:default;padding:.25rem .75rem}' +
      '.ohc-list mark{padding:0;background:#ffe58f}' +
      '@media (max-width:576px){.ohc-list{max-height:45vh}.ohc-list .list-group-item{padding:.6rem .75rem}}';
    document.head.appendChild(st);
  }

  function wants(sel) {
    if (sel.dataset.comboDone || sel.multiple || (sel.size && sel.size > 1)) return false;
    if (sel.hasAttribute('data-no-combo')) return false;
    if (sel.classList.contains('visually-hidden')) return false;
    if (sel.closest('.input-group')) return false;
    if (sel.hasAttribute('data-combo')) return true;
    if (PICKER_NAMES.test(sel.name || '') || PICKER_IDS.test(sel.id || '')) return true;
    var real = Array.prototype.filter.call(sel.options, function (o) { return o.value !== ''; }).length;
    return real >= MIN_OPTIONS;
  }

  function norm(t) { return (t || '').toLowerCase(); }
  function esc(t) {
    return String(t).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; });
  }
  function hl(text, q) {
    if (!q) return esc(text);
    var at = norm(text).indexOf(q);
    if (at === -1) return esc(text);
    return esc(text.slice(0, at)) + '<mark>' + esc(text.slice(at, at + q.length)) + '</mark>' + esc(text.slice(at + q.length));
  }

  var uid = 0;
  function enhance(sel) {
    sel.dataset.comboDone = '1';
    uid += 1;
    var small = sel.classList.contains('form-select-sm');
    var wrap = document.createElement('div');
    wrap.className = 'ohc';
    // keep the select's own spacing/width so the layout doesn't shift
    ['mb-1', 'mb-2', 'mb-3', 'mt-1', 'mt-2', 'w-auto', 'w-100'].forEach(function (c) {
      if (sel.classList.contains(c)) wrap.classList.add(c);
    });
    ['width', 'maxWidth', 'minWidth'].forEach(function (p) { if (sel.style[p]) wrap.style[p] = sel.style[p]; });
    if (sel.style.width === 'auto') { wrap.style.width = ''; wrap.style.minWidth = '200px'; }

    var group = document.createElement('div');
    group.className = 'input-group' + (small ? ' input-group-sm' : '');
    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'form-control';
    input.autocomplete = 'off';
    input.setAttribute('autocapitalize', 'off');
    input.setAttribute('spellcheck', 'false');
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'false');
    var listId = 'ohc-list-' + uid;
    input.setAttribute('aria-controls', listId);
    if (sel.id) {
      // clicking the field's <label for="..."> should open the picker
      var lab = document.querySelector('label[for="' + sel.id + '"]');
      if (lab) { input.id = sel.id + '-combo'; lab.htmlFor = input.id; }
    }
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-outline-secondary';
    btn.tabIndex = -1;
    btn.setAttribute('aria-label', 'Show all choices');
    btn.innerHTML = '<i class="bi bi-chevron-down"></i>';
    group.appendChild(input);
    group.appendChild(btn);
    var list = document.createElement('div');
    list.className = 'list-group shadow ohc-list';
    list.id = listId;
    list.setAttribute('role', 'listbox');
    wrap.appendChild(group);
    wrap.appendChild(list);

    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(sel);
    sel.classList.add('visually-hidden');
    sel.tabIndex = -1;
    sel.setAttribute('aria-hidden', 'true');
    var wasRequired = sel.required;
    sel.required = false;  // checked on submit below (a hidden required field can't show the browser's message)

    var shown = [], active = -1;

    function readOpts() {
      var out = [];
      Array.prototype.forEach.call(sel.options, function (o) {
        if (o.hidden) return;
        var g = o.parentNode && o.parentNode.tagName === 'OPTGROUP' ? o.parentNode.label : '';
        out.push({ value: o.value, text: o.text, off: o.disabled, group: g });
      });
      return out;
    }
    function placeholderText() {
      var first = sel.options[0];
      if (first && first.value === '' && first.disabled) return first.text.replace(/^[-\s]+|[-\s]+$/g, '');
      return 'Type or pick...';
    }
    function selectedText() {
      var o = sel.options[sel.selectedIndex];
      if (!o) return '';
      if (o.value === '' && o.disabled) return '';
      return o.text;
    }
    function render(q) {
      q = norm(q).trim();
      var all = readOpts();
      // a disabled blank placeholder ("-- pick one --") isn't a choice
      all = all.filter(function (o) { return !(o.value === '' && o.off); });
      shown = all.filter(function (o) { return !q || norm(o.text).indexOf(q) !== -1 || norm(o.group).indexOf(q) !== -1; });
      list.innerHTML = '';
      var lastGroup = null, idx = 0;
      shown.forEach(function (o) {
        if (o.group && o.group !== lastGroup) {
          var gh = document.createElement('div');
          gh.className = 'list-group-item ohc-grp';
          gh.textContent = o.group;
          gh.addEventListener('mousedown', function (e) { e.preventDefault(); });
          list.appendChild(gh);
        }
        lastGroup = o.group;
        var b = document.createElement('div');
        b.className = 'list-group-item list-group-item-action' + (o.value === sel.value ? ' ohc-chosen' : '') + (o.off ? ' ohc-off' : '');
        b.setAttribute('role', 'option');
        b.dataset.idx = idx;
        b.innerHTML = hl(o.text, q) || '&nbsp;';
        b.addEventListener('mousedown', function (e) { e.preventDefault(); if (!o.off) choose(o.value); });
        list.appendChild(b);
        idx += 1;
      });
      if (!shown.length) {
        var none = document.createElement('div');
        none.className = 'list-group-item text-muted small';
        none.textContent = q ? 'No matches for "' + input.value.trim() + '"' : 'Nothing to pick';
        list.appendChild(none);
      }
      active = -1;
      for (var i = 0; i < shown.length; i++) {
        if (q ? !shown[i].off : shown[i].value === sel.value) { active = i; break; }
      }
      paint();
    }
    function paint() {
      Array.prototype.forEach.call(list.querySelectorAll('[role="option"]'), function (el) {
        var on = +el.dataset.idx === active;
        el.classList.toggle('ohc-act', on);
        if (on) el.scrollIntoView({ block: 'nearest' });
      });
    }
    function isOpen() { return list.classList.contains('open'); }
    function open(q) { render(q); list.classList.add('open'); input.setAttribute('aria-expanded', 'true'); }
    function close() { list.classList.remove('open'); input.setAttribute('aria-expanded', 'false'); }
    function choose(value) {
      var changed = sel.value !== value;
      sel.value = value;
      input.value = selectedText();
      lastSeen = sel.value;
      input.classList.remove('is-invalid');
      close();
      if (changed) {
        sel.dispatchEvent(new Event('input', { bubbles: true }));
        sel.dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
    function move(step) {
      if (!shown.length) return;
      var i = active;
      for (var n = 0; n < shown.length; n++) {
        i = i < 0 ? (step > 0 ? 0 : shown.length - 1) : Math.min(shown.length - 1, Math.max(0, i + step));
        if (!shown[i].off) break;
      }
      active = i;
      paint();
    }

    input.addEventListener('focus', function () { input.select(); open(''); });
    input.addEventListener('click', function () { if (!isOpen()) { input.select(); open(''); } });
    input.addEventListener('input', function () { open(input.value); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (!isOpen()) { open(input.value); return; }
        move(e.key === 'ArrowDown' ? 1 : -1);
      } else if (e.key === 'Enter') {
        if (isOpen() && active >= 0 && shown[active] && !shown[active].off) { e.preventDefault(); choose(shown[active].value); }
        else if (isOpen()) { e.preventDefault(); }
      } else if (e.key === 'Escape') {
        if (isOpen()) { e.preventDefault(); e.stopPropagation(); }
        input.value = selectedText(); close();
      } else if (e.key === 'Tab') {
        close();
      }
    });
    input.addEventListener('blur', function () {
      setTimeout(function () { close(); input.value = selectedText(); }, 120);
    });
    btn.addEventListener('mousedown', function (e) { e.preventDefault(); });
    btn.addEventListener('click', function () { if (isOpen()) { close(); } else { input.focus(); if (!isOpen()) open(''); } });

    // Keep the box in step when page scripts change the select
    // (a scan picking the project, a preset, options rebuilt, shown/hidden).
    var lastSeen = sel.value;
    function mirror() {
      if (!document.body.contains(sel)) { clearInterval(timer); return; }
      if (sel.value !== lastSeen && document.activeElement !== input) {
        lastSeen = sel.value;
        input.value = selectedText();
      }
      input.disabled = sel.disabled;
      btn.disabled = sel.disabled;
      var hide = sel.style.display === 'none' || sel.hidden;
      wrap.style.display = hide ? 'none' : '';
    }
    sel.addEventListener('change', function () { if (document.activeElement !== input) { lastSeen = sel.value; input.value = selectedText(); } });
    var timer = setInterval(mirror, 400);
    if (window.MutationObserver) {
      new MutationObserver(function () {
        if (document.activeElement !== input) input.value = selectedText();
        if (isOpen()) render(input.value === selectedText() ? '' : input.value);
      }).observe(sel, { childList: true, subtree: true });
    }

    var form = sel.form;
    if (form && wasRequired) {
      form.addEventListener('submit', function (e) {
        if (!sel.value && !sel.disabled && wrap.offsetParent !== null) {
          e.preventDefault(); e.stopImmediatePropagation();
          input.classList.add('is-invalid');
          input.focus();
        }
      }, true);
    }
    // a form reset puts the select back - follow it
    if (form) form.addEventListener('reset', function () { setTimeout(function () { lastSeen = sel.value; input.value = selectedText(); }, 0); });

    input.placeholder = placeholderText();
    input.value = selectedText();
    mirror();
  }

  function scan(root) {
    if (!root || !root.querySelectorAll) return;
    var sels = root.tagName === 'SELECT' ? [root] : root.querySelectorAll('select');
    Array.prototype.forEach.call(sels, function (s) { if (wants(s)) enhance(s); });
  }

  function start() {
    addStyles();
    scan(document);
    // parts of pages refreshed live (schedule, dashboard) or opened later
    if (window.MutationObserver) {
      new MutationObserver(function (muts) {
        muts.forEach(function (m) {
          Array.prototype.forEach.call(m.addedNodes, function (n) { if (n.nodeType === 1 && !n.closest('.ohc')) scan(n); });
        });
      }).observe(document.body, { childList: true, subtree: true });
    }
  }
  window.OpsHubCombo = { enhance: function (sel) { addStyles(); if (!sel.dataset.comboDone) enhance(sel); } };
  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', start); } else { start(); }
})();
