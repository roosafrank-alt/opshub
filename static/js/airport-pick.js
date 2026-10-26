/* Logbook entry: airport type-ahead on From / To, and the flight-path map.
 * - Typing in From / To filters the airport list on every character (code or name).
 *   Frequented airports come first, then closest to home (or to the departure airport).
 * - The map draws a straight line (From -> To) in nautical miles; if a Route is filled in
 *   it follows the waypoints / airways instead. */
(function () {
  var from = document.querySelector('[data-apt=from]'), to = document.querySelector('[data-apt=to]'),
      route = document.querySelector('[data-apt=route]');
  if (!from || !to) return;
  var base = '/academy/api/';
  function tokenInfo(inp) { // the code being typed = text after the last separator
    var v = inp.value, m = v.match(/^(.*?)([A-Za-z0-9]*)$/);
    return { head: m[1], tok: m[2] };
  }
  function attach(inp, role) {
    var box = document.createElement('div');
    box.className = 'list-group shadow-sm position-absolute w-100 d-none';
    box.style.cssText = 'z-index:1050; max-height:260px; overflow-y:auto; top:100%; left:0';
    inp.parentNode.style.position = 'relative';
    inp.parentNode.appendChild(box);
    var seq = 0;
    function hide() { box.classList.add('d-none'); box.innerHTML = ''; }
    inp.addEventListener('input', function () {
      var t = tokenInfo(inp).tok, n = ++seq;
      if (!t) { hide(); return; }
      var url = base + 'airports?q=' + encodeURIComponent(t) + '&role=' + role +
        '&dep=' + encodeURIComponent(tokenInfo(from).tok || from.value.trim()) +
        (new URLSearchParams(location.search).get('student_id') ? '&student_id=' + new URLSearchParams(location.search).get('student_id') : '');
      fetch(url, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (list) {
        if (n !== seq) return;
        box.innerHTML = '';
        list.forEach(function (a) {
          var b = document.createElement('button');
          b.type = 'button'; b.className = 'list-group-item list-group-item-action py-1 small';
          var where = [a.city, a.region].filter(Boolean).join(', ');
          b.innerHTML = '<b style="color:#8a1c7c;font-family:ui-monospace,Menlo,monospace"></b> <span></span>' +
            (a.nm != null ? ' <span class="text-muted float-end"></span>' : '');
          b.children[0].textContent = a.code; b.children[1].textContent = (a.name || '') + (where ? ' - ' + where : '');
          if (a.nm != null) b.children[2].textContent = a.nm + ' nm';
          b.addEventListener('mousedown', function (ev) {
            ev.preventDefault();
            var h = tokenInfo(inp).head;
            inp.value = h + a.code;
            hide(); update();
          });
          box.appendChild(b);
        });
        box.classList.toggle('d-none', !list.length);
      });
    });
    inp.addEventListener('blur', function () { setTimeout(hide, 150); });
    inp.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !box.classList.contains('d-none') && box.firstChild) {
        ev.preventDefault(); box.firstChild.dispatchEvent(new MouseEvent('mousedown'));
      } else if (ev.key === 'Escape') hide();
    });
  }
  attach(from, 'from'); attach(to, 'to');

  var mapEl = document.getElementById('route-map');
  if (typeof L === 'undefined' || !window.AviationMap) { mapEl.innerHTML = '<div class="p-3 text-muted">The map could not load (no internet?).</div>'; return; }
  var av = window.AviationMap.create(mapEl, {
    onPick: function (role, code) {   // clicked a symbol on the map
      if (role === 'from') from.value = code;
      else if (role === 'to') to.value = code;
      else if (route) route.value = (route.value.trim() + ' ' + code).trim();
      update();
    }
  });
  var timer, seq = 0;
  function update() {
    clearTimeout(timer);
    timer = setTimeout(function () {
      var n = ++seq;
      var q = 'from=' + encodeURIComponent(from.value) + '&to=' + encodeURIComponent(to.value) +
              '&route=' + encodeURIComponent(route ? route.value : '');
      fetch(base + 'route?' + q, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
        if (n !== seq) return;
        var pts = d.points || [], nmEl = document.getElementById('route-nm'), note = document.getElementById('route-note'),
            tbl = document.getElementById('route-legs');
        tbl.innerHTML = '';
        if (d.error) { nmEl.textContent = ''; note.textContent = d.error; return; }
        if (pts.length < 2) { av.drawRoute({ points: [] }); nmEl.textContent = ''; note.textContent = 'Pick a From and To airport to see the path.'; return; }
        av.drawRoute(d);
        nmEl.textContent = d.nm + ' nm';
        note.textContent = (d.straight ? 'Straight line (great circle), true courses.' : 'Along the route entered, true courses.') +
          (d.unresolved && d.unresolved.length ? ' Not found: ' + d.unresolved.join(', ') + '.' : '');
        if (!d.straight && d.legs_info) {
          var h = '<table class="table table-sm mb-0"><thead><tr><th>Leg</th><th>Via</th><th class="text-end">Course</th><th class="text-end">nm</th></tr></thead><tbody>';
          d.legs_info.forEach(function (l) { h += '<tr><td>' + l.from + ' &rarr; ' + l.to + '</td><td>' + (l.via || 'direct') + '</td><td class="text-end">' + l.course + '&deg;</td><td class="text-end">' + l.nm.toFixed(1) + '</td></tr>'; });
          tbl.innerHTML = h + '<tr class="fw-bold"><td colspan="3">Total</td><td class="text-end">' + d.nm.toFixed(1) + '</td></tr></tbody></table>';
        }
      });
    }, 250);
  }
  [from, to, route].forEach(function (el) { if (el) el.addEventListener('input', update); });
  update();
})();
