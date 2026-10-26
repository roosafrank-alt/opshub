/* Aviation map for the logbook entry page.
 * Base: street map in greys, with the FAA VFR Sectional chart on top once zoomed in (zoom 8-12).
 * Symbols come from the nav database for whatever is on screen: airports (blue = towered, magenta = not),
 * VORs / NDBs, and waypoints when zoomed in. Click one to set From, To, or add it to the route.
 * The route is drawn as a great circle; each leg is labelled with its distance (nm) and true course. */
window.AviationMap = (function () {
  var MAG = '#8a1c7c', BLUE = '#1b4f9c', INK = '#2b2b2b';
  var base = '/academy/api/';

  function greatCircle(a, b, n) { // [lat,lon] -> list of [lat,lon] along the shortest path
    var r = Math.PI / 180, la1 = a[0] * r, lo1 = a[1] * r, la2 = b[0] * r, lo2 = b[1] * r;
    var d = 2 * Math.asin(Math.sqrt(Math.pow(Math.sin((la2 - la1) / 2), 2) + Math.cos(la1) * Math.cos(la2) * Math.pow(Math.sin((lo2 - lo1) / 2), 2)));
    if (d < 1e-9) return [a, b];
    var out = [];
    for (var i = 0; i <= n; i++) {
      var f = i / n, A = Math.sin((1 - f) * d) / Math.sin(d), B = Math.sin(f * d) / Math.sin(d);
      var x = A * Math.cos(la1) * Math.cos(lo1) + B * Math.cos(la2) * Math.cos(lo2);
      var y = A * Math.cos(la1) * Math.sin(lo1) + B * Math.cos(la2) * Math.sin(lo2);
      var z = A * Math.sin(la1) + B * Math.sin(la2);
      out.push([Math.atan2(z, Math.sqrt(x * x + y * y)) / r, Math.atan2(y, x) / r]);
    }
    return out;
  }

  function icon(html, size) { return L.divIcon({ className: 'avm-ic', html: html, iconSize: [size, size], iconAnchor: [size / 2, size / 2] }); }
  function airportIcon(a) {
    var c = a.tower ? BLUE : MAG, big = a.type === 'large_airport' || a.type === 'medium_airport', s = big ? 16 : 11;
    if (a.type === 'heliport') return icon('<div style="width:11px;height:11px;border:2px solid ' + c + ';border-radius:50%;background:#fff;font:700 8px/7px sans-serif;color:' + c + ';text-align:center">H</div>', 11);
    return icon('<div style="width:' + s + 'px;height:' + s + 'px;border:2px solid ' + c + ';border-radius:50%;background:rgba(255,255,255,.85)"></div>', s);
  }
  var NAV_SVG = '<svg width="22" height="22" viewBox="-11 -11 22 22"><polygon points="0,-9 7.8,-4.5 7.8,4.5 0,9 -7.8,4.5 -7.8,-4.5" fill="rgba(255,255,255,.7)" stroke="' + BLUE + '" stroke-width="1.6"/><circle r="1.6" fill="' + BLUE + '"/></svg>';
  var NDB_SVG = '<svg width="16" height="16" viewBox="-8 -8 16 16"><circle r="6" fill="none" stroke="' + MAG + '" stroke-width="1.5" stroke-dasharray="2 2"/><circle r="1.6" fill="' + MAG + '"/></svg>';
  var FIX_SVG = '<svg width="12" height="12" viewBox="-6 -6 12 12"><polygon points="0,-5 5,4 -5,4" fill="rgba(255,255,255,.8)" stroke="' + INK + '" stroke-width="1.3"/></svg>';

  function create(el, opts) {
    var onPick = opts.onPick || function () {};
    var map = L.map(el, { zoomControl: true, preferCanvas: false }).setView([41.72778, -74.37722], 8);
    // Sharp by design: the street map is requested at double resolution on phones (detectRetina), and every aviation
    // symbol / airway is drawn as vector graphics from our own database, so nothing here depends on how sharp
    // someone else's chart tiles are. The FAA sectional is only an optional overlay (see the layers button).
    var osm = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png';
    var street = L.tileLayer(osm, { maxZoom: 18, maxNativeZoom: 18, detectRetina: true, attribution: '&copy; OpenStreetMap', className: 'avm-grey' }).addTo(map);
    var streetPlain = L.tileLayer(osm, { maxZoom: 18, maxNativeZoom: 18, detectRetina: true, attribution: '&copy; OpenStreetMap' });
    var sectional = L.tileLayer('https://tiles.arcgis.com/tiles/ssFJjBXIUyZDrSYZ/arcgis/rest/services/VFR_Sectional/MapServer/tile/{z}/{y}/{x}', {
      minZoom: 8, minNativeZoom: 8, maxNativeZoom: 12, maxZoom: 18, opacity: .85, attribution: 'FAA VFR Sectional' });
    var airwayLayer = L.layerGroup().addTo(map);
    var symbols = L.layerGroup().addTo(map), routeLayer = L.layerGroup().addTo(map);
    L.control.layers({ 'Street map (grey)': street, 'Street map (colour)': streetPlain },
      { 'Airports, navaids, waypoints': symbols, 'Airways (V, J, T, Q)': airwayLayer, 'FAA sectional (may look soft in places)': sectional },
      { position: 'topright', collapsed: true }).addTo(map);
    // scale bar in nautical miles
    var Scale = L.Control.extend({ options: { position: 'bottomleft' }, onAdd: function () {
      var d = L.DomUtil.create('div', 'avm-scale'); this._d = d; map.on('move zoom', this._u, this); this._u(); return d; },
      _u: function () {
        var y = map.getSize().y / 2, m = map.distance(map.containerPointToLatLng([10, y]), map.containerPointToLatLng([110, y])), nm = m / 1852;
        var steps = [0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500], n = steps[0];
        steps.forEach(function (s) { if (s <= nm) n = s; });
        this._d.innerHTML = '<div style="width:' + Math.round(100 * n / nm) + 'px"></div>' + n + ' nm'; } });
    new Scale().addTo(map);

    function popup(item, kind) {
      var h = document.createElement('div');
      var title = '<b style="color:' + MAG + '">' + item.code + '</b> ' + (item.name || '');
      var sub = kind === 'airport' ? [item.city, item.elev != null ? item.elev + ' ft' : '', item.tower ? 'towered' : 'non-towered'].filter(Boolean).join(' · ')
              : kind === 'navaid' ? [item.type, item.freq].filter(Boolean).join(' ') : 'Waypoint';
      h.innerHTML = '<div style="min-width:170px">' + title + '<div class="text-muted small">' + sub + '</div></div>';
      var row = document.createElement('div'); row.style.marginTop = '4px';
      [['From', 'from'], ['To', 'to'], ['+ Route', 'via']].forEach(function (b) {
        if (b[1] !== 'via' && kind !== 'airport') return;
        var btn = document.createElement('button'); btn.type = 'button'; btn.textContent = b[0];
        btn.className = 'btn btn-outline-primary btn-sm py-0 me-1'; btn.onclick = function () { onPick(b[1], item.code); map.closePopup(); };
        row.appendChild(btn);
      });
      h.appendChild(row); return h;
    }
    var seq = 0;
    function refresh() {
      if (!map.hasLayer(symbols) && !map.hasLayer(airwayLayer)) { return; }
      var b = map.getBounds(), n = ++seq;
      fetch(base + 'nav?south=' + b.getSouth() + '&west=' + b.getWest() + '&north=' + b.getNorth() + '&east=' + b.getEast() + '&zoom=' + map.getZoom(), { credentials: 'same-origin' })
        .then(function (r) { return r.json(); }).then(function (d) {
          if (n !== seq) return;
          symbols.clearLayers(); airwayLayer.clearLayers();
          var z = map.getZoom(), labelled = {};
          (d.airways || []).forEach(function (w) {
            var col = w.high ? '#8a6d3b' : '#3a6ea5';
            var ln = L.polyline([w.a, w.b], { color: col, weight: w.high ? 1.4 : 1.8, opacity: .75, dashArray: w.high ? '2 5' : null, interactive: false }).addTo(airwayLayer);
            var px = map.latLngToLayerPoint(w.a).distanceTo(map.latLngToLayerPoint(w.b));
            if (z >= 9 && px > 110 && !labelled[w.id + Math.round(w.a[0] * 4)]) {
              labelled[w.id + Math.round(w.a[0] * 4)] = 1;
              L.marker([(w.a[0] + w.b[0]) / 2, (w.a[1] + w.b[1]) / 2], { icon: L.divIcon({ className: 'avm-awy', html: '<span style="color:' + col + '">' + w.id + '</span>', iconSize: null }), interactive: false }).addTo(airwayLayer);
            }
          });
          (d.fixes || []).forEach(function (f) {
            var m = L.marker([f.lat, f.lon], { icon: icon(FIX_SVG, 12), opacity: .65 }).addTo(symbols);
            if (z >= 12) m.bindTooltip(f.code, { permanent: true, direction: 'right', className: 'avm-lbl', offset: [6, 0] });
            m.bindPopup(function () { return popup(f, 'fix'); });
          });
          (d.navaids || []).forEach(function (v) {
            var vor = /VOR|TACAN/.test(v.type || '');
            var m = L.marker([v.lat, v.lon], { icon: icon(vor ? NAV_SVG : NDB_SVG, vor ? 22 : 16) }).addTo(symbols);
            m.bindTooltip(v.code, { permanent: z >= 8, direction: 'right', className: 'avm-lbl', offset: [10, 0] });
            m.bindPopup(function () { return popup(v, 'navaid'); });
          });
          (d.airports || []).forEach(function (a) {
            var m = L.marker([a.lat, a.lon], { icon: airportIcon(a) }).addTo(symbols);
            m.bindTooltip(a.code, { permanent: z >= 8, direction: 'right', className: 'avm-lbl' + (a.tower ? ' tw' : ''), offset: [8, 0] });
            m.bindPopup(function () { return popup(a, 'airport'); });
          });
        });
    }
    var t; map.on('moveend', function () { clearTimeout(t); t = setTimeout(refresh, 200); });
    map.on('overlayadd overlayremove', refresh);
    refresh();

    function drawRoute(d) {
      routeLayer.clearLayers();
      var pts = d.points || [];
      if (pts.length < 2) return;
      var all = [];
      var info = d.legs_info || [];
      for (var i = 0; i < pts.length - 1; i++) {
        var seg = greatCircle([pts[i].lat, pts[i].lon], [pts[i + 1].lat, pts[i + 1].lon], 24);
        L.polyline(seg, { color: MAG, weight: 4, opacity: .9, dashArray: d.straight ? '8 7' : null }).addTo(routeLayer);
        all = all.concat(seg);
        var mid = seg[Math.floor(seg.length / 2)], lg = info[i];
        var px = map.latLngToLayerPoint([pts[i].lat, pts[i].lon]).distanceTo(map.latLngToLayerPoint([pts[i + 1].lat, pts[i + 1].lon]));
        if (lg && px > 130) {   // short legs are in the table below; labels would pile up
          var txt = (lg.via ? '<b>' + lg.via + '</b> ' : '') + lg.course + '° · ' + Math.round(lg.nm) + ' nm';
          L.marker(mid, { icon: L.divIcon({ className: 'avm-leg', html: '<span>' + txt + '</span>', iconSize: null }), interactive: false }).addTo(routeLayer);
        }
      }
      pts.forEach(function (p, i) {
        var end = i === 0 || i === pts.length - 1;
        L.circleMarker([p.lat, p.lon], { radius: end ? 8 : 5, color: MAG, weight: 3, fillColor: end ? '#fff' : MAG, fillOpacity: 1 })
          .bindTooltip(p.code, { permanent: true, direction: 'top', className: 'avm-lbl strong', offset: [0, -6] }).addTo(routeLayer);
      });
      map.fitBounds(all, { padding: [50, 50], maxZoom: 11 });
    }
    return { map: map, drawRoute: drawRoute, resize: function () { map.invalidateSize(); } };
  }
  return { create: create };
})();
