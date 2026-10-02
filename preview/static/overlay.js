/* OpsHub preview overlay: top bar, outlines + chips on every element a change touched, per-page change list. */
(function () {
  if (window.__pvLoaded) return; window.__pvLoaded = true;
  var script = document.currentScript || document.querySelector('script[data-pv-label]');
  var label = (script && script.getAttribute('data-pv-label')) || 'preview';
  var inFrame = window.self !== window.top;
  var data = null, hlOn = true, layer = null, panel = null, bubble = null, bar = null;
  try { hlOn = localStorage.getItem('pv-hl') !== '0'; } catch (e) {}

  var css = '#pv-bar{background:#a0125f;color:#fff;font:600 13px/1.2 system-ui,sans-serif;padding:6px 10px;display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center;position:relative;z-index:2147482000}' +
    '#pv-bar.before{background:#374151}' +
    '#pv-bar a,#pv-bar button{color:#fff;background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.45);border-radius:999px;padding:3px 10px;font:600 12px system-ui,sans-serif;text-decoration:none;cursor:pointer}' +
    '#pv-bar .sp{flex:1}' +
    '.pv-hl{outline:2px dashed #e0197d !important;outline-offset:2px}' +
    '.pv-flash{animation:pvflash 1.2s ease 2}@keyframes pvflash{50%{outline-color:#ffd400;outline-width:5px}}' +
    '#pv-layer{position:absolute;left:0;top:0;width:0;height:0;z-index:2147482500}' +
    '.pv-chip{position:absolute;transform:translateY(-100%);background:#e0197d;color:#fff;font:700 10px/1 system-ui,sans-serif;padding:3px 6px;border-radius:6px 6px 6px 0;border:0;cursor:pointer;white-space:nowrap;box-shadow:0 1px 4px rgba(0,0,0,.35)}' +
    '#pv-panel,#pv-bubble{position:fixed;left:8px;right:8px;bottom:8px;max-width:520px;margin:0 auto;background:#fff;color:#16202e;border:2px solid #a0125f;border-radius:12px;padding:12px 14px;font:14px/1.4 system-ui,sans-serif;z-index:2147483000;box-shadow:0 8px 30px rgba(0,0,0,.35);max-height:70vh;overflow:auto;display:none}' +
    '#pv-panel h4,#pv-bubble h4{margin:0 0 6px;font-size:15px}' +
    '#pv-panel .row{display:flex;gap:8px;align-items:baseline;padding:7px 0;border-top:1px solid #e5e7eb;cursor:pointer}' +
    '#pv-panel .dot{width:10px;height:10px;border-radius:50%;flex:none;margin-top:3px}' +
    '.pv-built{background:#16a34a}.pv-logic{background:#2563eb}.pv-partial{background:#d97706}.pv-skipped{background:#9ca3af}.pv-pending{background:#fff;border:2px solid #9ca3af}' +
    '#pv-bubble .lab{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#6b7280;margin-top:8px}' +
    '#pv-bubble .aft .lab{color:#15803d}#pv-bubble .x,#pv-panel .x{float:right;border:0;background:#eee;border-radius:50%;width:28px;height:28px;font-size:16px;cursor:pointer}' +
    '#pv-bubble .note{margin-top:8px;padding:6px 8px;background:#fef3c7;border-radius:8px}';
  var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
  function re(p) { return new RegExp('^' + p.replace(/[.+?^$()|[\]\\]/g, '\\$&').replace(/\{[a-z_]+\}/g, '[^/]+').replace(/\*/g, '.*') + '$'); }
  function onThisPage(c) {
    var p = location.pathname, found = !!document.querySelector('[data-change~="' + c.id + '"]');
    if (found) return true;
    for (var i = 0; i < (c.pages || []).length; i++) { try { if (re(c.pages[i]).test(p)) return true; } catch (e) {} }
    return false;
  }

  function buildBar() {
    bar = document.createElement('div'); bar.id = 'pv-bar'; bar.className = label === 'before' ? 'before' : '';
    var host = location.pathname + location.search;
    if (label === 'before') {
      bar.innerHTML = '<span>BEFORE: today\'s live code on a copy of the data</span><span class="sp"></span>' +
        '<a href="' + location.protocol + '//' + location.hostname + ':' + data.preview_port + host + '">See this page in the preview</a>';
    } else {
      bar.innerHTML = '<span>PREVIEW COPY: not the live site. Emails, texts, phone alerts and Wave are off.</span><span class="sp"></span>' +
        '<button id="pv-hl-btn"></button><button id="pv-list-btn"></button>' +
        '<a href="/__preview/compare?path=' + encodeURIComponent(host) + '">Side by side</a>' +
        '<a href="/__preview/all">All changes</a>' +
        '<a href="/__preview/outbox">Blocked sends' + (data.outbox.length ? ' (' + data.outbox.length + ')' : '') + '</a>';
    }
    document.body.insertBefore(bar, document.body.firstChild);
    var hb = document.getElementById('pv-hl-btn'); if (hb) hb.onclick = function () { hlOn = !hlOn; try { localStorage.setItem('pv-hl', hlOn ? '1' : '0'); } catch (e) {} paint(); };
    var lb = document.getElementById('pv-list-btn'); if (lb) lb.onclick = function () { togglePanel(); };
  }

  function paint() {
    var els = document.querySelectorAll('[data-change]');
    els.forEach(function (el) { el.classList.toggle('pv-hl', hlOn); });
    if (!layer) { layer = document.createElement('div'); layer.id = 'pv-layer'; document.body.appendChild(layer); }
    layer.innerHTML = '';
    if (hlOn) {
      els.forEach(function (el) {
        var r = el.getBoundingClientRect(); if (!r.width || !r.height) return;
        var ids = (el.getAttribute('data-change') || '').split(/\s+/).filter(Boolean);
        var b = document.createElement('button'); b.className = 'pv-chip'; b.textContent = ids.join(' ');
        var barEl = document.getElementById('pv-bar'), barH = barEl ? barEl.offsetHeight : 0, topY = r.top + window.scrollY;
        b.style.left = (r.left + window.scrollX) + 'px'; b.style.top = (topY - 1) + 'px';
        if (topY < barH + 14) { b.style.top = (topY + 1) + 'px'; b.style.transform = 'none'; b.style.borderRadius = '0 0 6px 0'; }   /* near the top bar: tuck the chip inside the outline */
        b.onclick = function (ev) { ev.preventDefault(); ev.stopPropagation(); showBubble(ids[0]); };
        layer.appendChild(b);
      });
    }
    var hb = document.getElementById('pv-hl-btn'); if (hb) hb.textContent = 'Highlights ' + (hlOn ? 'ON' : 'OFF');
    var mine = data.changes.filter(onThisPage);
    var lb = document.getElementById('pv-list-btn'); if (lb) lb.textContent = mine.length + ' change' + (mine.length === 1 ? '' : 's') + ' on this page';
  }

  function byId(id) { return data.changes.filter(function (c) { return c.id === id; })[0]; }
  function showBubble(id) {
    var c = byId(id); if (!c) return;
    if (!bubble) { bubble = document.createElement('div'); bubble.id = 'pv-bubble'; document.body.appendChild(bubble); }
    bubble.innerHTML = '<button class="x" aria-label="Close">×</button><h4>' + esc(c.title) + ' <small style="color:#6b7280">' + esc(c.id) + '</small></h4>' +
      '<div class="lab">Before</div><div>' + esc(c.before) + '</div>' +
      '<div class="aft"><div class="lab">After</div><div>' + esc(c.note || c.after) + '</div></div>' +
      (c.detail ? '<div class="note">' + esc(c.detail) + '</div>' : '') +
      (c.where ? '<div class="note">Where to look: ' + esc(c.where) + '</div>' : '') +
      (c.see_as ? '<div class="note">Log in as: ' + esc(c.see_as) + '</div>' : '');
    bubble.style.display = 'block'; if (panel) panel.style.display = 'none';
    bubble.querySelector('.x').onclick = function () { bubble.style.display = 'none'; };
  }
  function togglePanel() {
    if (!panel) { panel = document.createElement('div'); panel.id = 'pv-panel'; document.body.appendChild(panel); }
    if (panel.style.display === 'block') { panel.style.display = 'none'; return; }
    var mine = data.changes.filter(onThisPage);
    panel.innerHTML = '<button class="x" aria-label="Close">×</button><h4>Changes on this page (' + mine.length + ')</h4>' +
      (mine.length ? '' : '<div>Nothing on this page was changed. Use "All changes" in the top bar to see everything in this build.</div>') +
      mine.map(function (c) { return '<div class="row" data-id="' + esc(c.id) + '"><span class="dot pv-' + esc(c.status) + '"></span><span><b>' + esc(c.id) + '</b> ' + esc(c.title) + '</span></div>'; }).join('') +
      '<div style="margin-top:8px;font-size:12px;color:#6b7280">Green: you can see it here. Blue: works behind the scenes. Amber: partly done. Grey: not built, see the note.</div>';
    panel.style.display = 'block'; if (bubble) bubble.style.display = 'none';
    panel.querySelector('.x').onclick = function () { panel.style.display = 'none'; };
    panel.querySelectorAll('.row').forEach(function (row) {
      row.onclick = function () {
        var id = row.getAttribute('data-id'), el = document.querySelector('[data-change~="' + id + '"]');
        if (el) { panel.style.display = 'none'; el.scrollIntoView({ behavior: 'smooth', block: 'center' }); el.classList.add('pv-flash'); setTimeout(function () { el.classList.remove('pv-flash'); }, 2600); }
        else showBubble(id);
      };
    });
  }

  fetch('/__preview/changes.json', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
    data = d;
    if (!inFrame) buildBar();
    if (label !== 'before') { paint(); var t = null; var again = function () { clearTimeout(t); t = setTimeout(paint, 300); };
      window.addEventListener('resize', again); document.addEventListener('click', again, true); setInterval(paint, 1500); }
  }).catch(function () {});
})();
