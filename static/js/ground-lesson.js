// GS-LESSONS / GS-INTERACTIVE / GS-GRADES: lesson page behavior.
// Quick checks are graded on the server (a wrong answer can be retried until right; the first try
// is what the instructor sees). Games report when finished. Active time and the reading spot are
// sent as a heartbeat so a student can resume where they stopped.
(function () {
  var root = document.getElementById('gsl');
  if (!root) return;
  var taskId = root.getAttribute('data-task-id');
  var base = '/flight/groundschool/task/' + taskId;
  function post(path, body) {
    return fetch(base + path, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    }).then(function (r) { return r.json(); });
  }

  // ---------- saved progress: finished games and quizzes show as completed, with a Retake button.
  // Retake reloads the page with a one-time flag so the item comes back fresh and playable.
  function retakeKey(el) {
    var k = el.getAttribute('data-key');
    if (!k) { var q = el.querySelector('.gsl-q'); k = 'qg' + el.getAttribute('data-final') + '-' + (q ? q.getAttribute('data-n') : '0'); }
    return 'gsl-retake-' + taskId + '-' + k;
  }
  function retakeBar(el, text) {
    var bar = document.createElement('div'); bar.className = 'gsl-donebar';
    var span = document.createElement('span'); span.innerHTML = '<i class="bi bi-check-circle-fill"></i> ' + text;
    var btn = document.createElement('button'); btn.type = 'button'; btn.className = 'gsl-btn gsl-retake'; btn.textContent = 'Retake';
    btn.addEventListener('click', function () {
      try { sessionStorage.setItem(retakeKey(el), '1'); } catch (e) {}
      location.reload();
    });
    bar.appendChild(span); bar.appendChild(btn); return bar;
  }
  function collapseIfDone(el, text) {
    if (el.getAttribute('data-done') !== '1') return;
    try { var k = retakeKey(el); if (sessionStorage.getItem(k)) { sessionStorage.removeItem(k); return; } } catch (e) {}
    var body = el.querySelector('.gsl-body'); if (body) body.hidden = true;
    el.appendChild(retakeBar(el, text));
  }

  // ---------- quick checks
  root.querySelectorAll('.gsl-qgroup').forEach(function (g) {
    var final = g.getAttribute('data-final') === '1';
    var qs = g.querySelectorAll('.gsl-q'), solved = 0, first = 0, firstTried = 0, score = g.querySelector('.gsl-score');
    collapseIfDone(g, 'Completed. First try: ' + g.getAttribute('data-first') + ' of ' + g.getAttribute('data-count') + ' right.');
    function show() {
      score.textContent = solved + ' of ' + qs.length + ' right' + (solved === qs.length ? ' · done' : ' so far') +
        (firstTried ? ' · first try: ' + first + ' of ' + firstTried : '');
    }
    qs.forEach(function (q) {
      var tries = 0, msg = document.createElement('div');
      msg.className = 'gsl-qmsg'; msg.setAttribute('aria-live', 'polite');
      q.querySelector('.gsl-opts').after(msg);
      q.querySelectorAll('button').forEach(function (btn) {
        btn.addEventListener('click', function () {
          if (q.getAttribute('data-done') || btn.disabled) return;
          var j = parseInt(btn.getAttribute('data-j'), 10), n = parseInt(q.getAttribute('data-n'), 10);
          btn.disabled = true;
          post('/quiz-answer', { n: n, j: j, final: final }).then(function (res) {
            if (!res || !res.ok) { btn.disabled = false; msg.textContent = 'Could not check that. Try again.'; return; }
            tries++;
            if (tries === 1) { firstTried++; if (res.correct) first++; }
            if (res.correct) {
              q.setAttribute('data-done', '1'); solved++;
              btn.classList.add('right');
              q.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
              msg.textContent = tries === 1 ? 'Right on the first try!' : 'Got it after ' + tries + ' tries.';
              var why = q.querySelector('.gsl-qwhy'); why.innerHTML = res.why; why.hidden = false;
            } else {
              btn.classList.add('wrong');
              msg.textContent = 'Not quite. Pick another answer.';
            }
            show();
            if (solved === qs.length && !g.querySelector('.gsl-donebar')) g.appendChild(retakeBar(g, 'Completed. First try: ' + first + ' of ' + qs.length + ' right.'));
          }).catch(function () { btn.disabled = false; msg.textContent = 'No connection. Try again.'; });
        });
      });
    });
  });

  // ---------- games
  function shuffle(box) {
    var kids = Array.prototype.slice.call(box.children);
    for (var i = kids.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1)), t = kids[i]; kids[i] = kids[j]; kids[j] = t;
    }
    kids.forEach(function (k) { box.appendChild(k); });
  }
  function status(act, msg) { act.querySelector('.gsl-act-status').textContent = msg; }
  function shake(el) { el.classList.add('gsl-bad'); setTimeout(function () { el.classList.remove('gsl-bad'); }, 450); }
  function finished(act, attempts) {   // attempts = 1 + mistakes
    var key = act.getAttribute('data-key');
    if (!act.querySelector('.gsl-donebar')) act.appendChild(retakeBar(act, attempts > 1 ? 'Finished after ' + (attempts - 1) + ' mistake' + (attempts > 2 ? 's' : '') + '.' : 'Finished with no mistakes.'));
    if (key && !act.getAttribute('data-reported')) {
      act.setAttribute('data-reported', '1');
      post('/game-done', { key: key, tries: attempts }).catch(function () {});
    }
  }

  root.querySelectorAll('.gsl-act').forEach(function (act) {
    var wrap = document.createElement('div'); wrap.className = 'gsl-body';
    Array.prototype.slice.call(act.children).forEach(function (c) {
      if (!c.classList.contains('gsl-act-head') && !c.classList.contains('gsl-act-status')) wrap.appendChild(c);
    });
    act.insertBefore(wrap, act.querySelector('.gsl-act-status'));
    act.querySelectorAll('[data-shuffle]').forEach(shuffle);
    if (act.getAttribute('data-done') === '1') act.setAttribute('data-reported', '1');
    var tr = parseInt(act.getAttribute('data-tries'), 10) || 1;
    collapseIfDone(act, tr > 1 ? 'Finished after ' + (tr - 1) + ' mistake' + (tr > 2 ? 's' : '') + '.' : 'Finished with no mistakes.');

    // match + label: tap one on each side, or drag one onto the other; same data-k is a pair.
    var pairing = act.querySelector('.gsl-pairing');
    if (pairing) {
      var total = parseInt(pairing.getAttribute('data-total'), 10), got = 0, miss = 0, sel = null;
      pairing.querySelectorAll('[data-k]').forEach(function (el) {
        el.addEventListener('click', function () {
          if (el.disabled) return;
          if (!sel || sel.getAttribute('data-side') === el.getAttribute('data-side')) {
            if (sel) sel.classList.remove('gsl-sel');
            sel = el; el.classList.add('gsl-sel'); return;
          }
          if (sel.getAttribute('data-k') === el.getAttribute('data-k')) {
            [sel, el].forEach(function (x) { x.classList.remove('gsl-sel'); x.classList.add('gsl-ok'); x.disabled = true; });
            got++;
            if (got === total) { status(act, 'All matched' + (miss ? ' with ' + miss + ' miss' + (miss > 1 ? 'es' : '') : ' with no misses') + '. Nice work!'); finished(act, 1 + miss); }
            else status(act, got + ' of ' + total + ' matched');
          } else {
            miss++; shake(sel); shake(el); sel.classList.remove('gsl-sel');
            status(act, 'Not that one. Try again.');
          }
          sel = null;
        });
      });
    }

    // sort: tap an item then its bucket, or drag the item into the bucket.
    var sort = act.querySelector('.gsl-sort');
    if (sort) {
      var stotal = parseInt(sort.getAttribute('data-total'), 10), sdone = 0, ssel = null, smiss = 0;
      sort.querySelectorAll('.gsl-item').forEach(function (it) {
        it.addEventListener('click', function () {
          if (ssel) ssel.classList.remove('gsl-sel');
          ssel = it; it.classList.add('gsl-sel');
        });
      });
      sort.querySelectorAll('.gsl-bucket').forEach(function (b) {
        b.addEventListener('click', function () {
          if (!ssel) { status(act, 'Tap or drag an item to a bucket.'); return; }
          if (ssel.getAttribute('data-b') === b.getAttribute('data-b')) {
            var chip = document.createElement('span'); chip.className = 'gsl-placed'; chip.innerHTML = (ssel.querySelector('.gsl-tile-t') || ssel).innerHTML;
            b.querySelector('.gsl-bucket-items').appendChild(chip);
            ssel.remove(); ssel = null; sdone++;
            if (sdone === stotal) { status(act, 'All sorted' + (smiss ? ' (' + smiss + ' miss' + (smiss > 1 ? 'es' : '') + ')' : ' with no misses') + '!'); finished(act, 1 + smiss); }
            else status(act, sdone + ' of ' + stotal + ' sorted');
          } else { smiss++; shake(ssel); shake(b); status(act, 'Not that bucket. Try again.'); }
        });
      });
    }

    // order: arrows, then check.
    var order = act.querySelector('.gsl-order');
    if (order) {
      var checks = 0;
      order.addEventListener('click', function (e) {
        var btn = e.target.closest('button'); if (!btn) return;
        var li = btn.closest('li');
        if (btn.classList.contains('gsl-up') && li.previousElementSibling) order.insertBefore(li, li.previousElementSibling);
        if (btn.classList.contains('gsl-down') && li.nextElementSibling) order.insertBefore(li.nextElementSibling, li);
        order.querySelectorAll('li').forEach(function (x) { x.classList.remove('gsl-ok', 'gsl-bad'); });
      });
      act.querySelector('.gsl-order-check').addEventListener('click', function () {
        var right = 0, items = order.querySelectorAll('li');
        checks++;
        items.forEach(function (li, i) {
          var ok = parseInt(li.getAttribute('data-pos'), 10) === i;
          li.classList.toggle('gsl-ok', ok); li.classList.toggle('gsl-bad', !ok);
          if (ok) right++;
        });
        if (right === items.length) { status(act, 'Perfect order!'); finished(act, checks); }
        else status(act, right + ' of ' + items.length + ' in the right spot. Keep moving them.');
      });
    }

    // true / false rapid-fire with a streak.
    var tf = act.querySelector('.gsl-tf');
    if (tf) {
      var cards = tf.querySelectorAll('.gsl-tfcard'), ci = 0, streak = 0, best = 0, score = 0, wrong = 0;
      var next = tf.querySelector('.gsl-tfnext');
      cards.forEach(function (c) {
        c.querySelectorAll('[data-v]').forEach(function (b) {
          b.addEventListener('click', function () {
            if (c.getAttribute('data-done')) return;
            c.setAttribute('data-done', '1');
            var ok = b.getAttribute('data-v') === c.getAttribute('data-a');
            b.classList.add(ok ? 'gsl-ok' : 'gsl-bad');
            if (ok) { score++; streak++; best = Math.max(best, streak); } else { streak = 0; wrong++; }
            c.querySelector('.gsl-tfwhy').hidden = false;
            status(act, (ok ? 'Right! ' : 'Not quite. ') + 'Streak ' + streak + ' · ' + score + ' of ' + cards.length + ' right');
            if (ci < cards.length - 1) { next.hidden = false; }
            else { status(act, 'Done: ' + score + ' of ' + cards.length + ' right. Best streak ' + best + '.'); finished(act, 1 + wrong); }
          });
        });
      });
      next.addEventListener('click', function () {
        cards[ci].hidden = true; ci++; cards[ci].hidden = false; next.hidden = true;
      });
    }

    // flashcards.
    var flash = act.querySelector('.gsl-flash');
    if (flash) {
      var fc = flash.querySelectorAll('.gsl-fcard'), fi = 0, count = flash.querySelector('.gsl-fcount');
      var show = function (n) {
        fc[fi].hidden = true; fi = (n + fc.length) % fc.length; fc[fi].hidden = false;
        fc[fi].querySelector('.gsl-ffront').hidden = false; fc[fi].querySelector('.gsl-fback').hidden = true;
        fc[fi].classList.remove('gsl-flipped'); count.textContent = (fi + 1) + ' / ' + fc.length;
      };
      fc.forEach(function (c) {
        c.addEventListener('click', function () {
          var f = c.querySelector('.gsl-ffront'), b = c.querySelector('.gsl-fback');
          f.hidden = !f.hidden; b.hidden = !b.hidden; c.classList.toggle('gsl-flipped');
        });
      });
      flash.querySelector('.gsl-fprev').addEventListener('click', function () { show(fi - 1); });
      flash.querySelector('.gsl-fnext').addEventListener('click', function () { show(fi + 1); });
      count.textContent = '1 / ' + fc.length;
    }
  });

  // ---------- drag (mouse, pen and touch): dropping A on B is the same as tapping A then B.
  var dragSuppress = false;
  document.addEventListener('click', function (e) {
    if (dragSuppress) { e.stopPropagation(); e.preventDefault(); dragSuppress = false; }
  }, true);
  root.querySelectorAll('[data-drag]').forEach(function (src) {
    src.addEventListener('pointerdown', function (e) {
      if (src.disabled || (e.pointerType === 'mouse' && e.button !== 0)) return;
      if (e.pointerType !== 'mouse' && !e.target.closest('.gsl-grip')) return;   // on a phone, drag by the grip so the page can still scroll
      var sx = e.clientX, sy = e.clientY, ghost = null, hot = null, act = src.closest('.gsl-act');
      function targetAt(x, y) {
        var el = document.elementFromPoint(x, y);
        var t = el && el.closest('[data-k],.gsl-bucket');
        return (t && t !== src && act.contains(t) && !t.disabled) ? t : null;
      }
      function move(ev) {
        if (!ghost) {
          if (Math.abs(ev.clientX - sx) + Math.abs(ev.clientY - sy) < 10) return;
          ghost = src.cloneNode(true); ghost.removeAttribute('data-k'); ghost.className += ' gsl-ghost';
          ghost.style.width = src.offsetWidth + 'px'; document.body.appendChild(ghost); src.classList.add('gsl-dragging');
        }
        ev.preventDefault();
        ghost.style.left = (ev.clientX - 20) + 'px'; ghost.style.top = (ev.clientY - 20) + 'px';
        var t = targetAt(ev.clientX, ev.clientY);
        if (hot && hot !== t) hot.classList.remove('gsl-drop');
        if (t) t.classList.add('gsl-drop');
        hot = t;
      }
      function up(ev) {
        document.removeEventListener('pointermove', move); document.removeEventListener('pointerup', up);
        document.removeEventListener('pointercancel', up);
        if (!ghost) return;                         // it was a plain tap; let the click happen
        var t = targetAt(ev.clientX, ev.clientY);
        ghost.remove(); src.classList.remove('gsl-dragging'); if (hot) hot.classList.remove('gsl-drop');
        if (t && ev.type === 'pointerup') { src.click(); t.click(); }
        dragSuppress = true; setTimeout(function () { dragSuppress = false; }, 60);   // swallow the browser's own click after the drop
      }
      document.addEventListener('pointermove', move, { passive: false });
      document.addEventListener('pointerup', up);
      document.addEventListener('pointercancel', up);
    });
  });

  // ---------- time spent + reading spot (students only)
  var anchors = [].slice.call(root.querySelectorAll('[id^="gsl-c"], #gsl-prepare, #gsl-apply, #gsl-checkride, #gsl-wrap'));
  anchors.sort(function (a, b) { return a.offsetTop - b.offsetTop; });
  function currentAnchor() {
    var best = '';
    anchors.forEach(function (a) { if (a.getBoundingClientRect().top < window.innerHeight * 0.5) best = a.id; });
    return best;
  }
  if (root.getAttribute('data-track') === '1') {
    var active = 0, lastInput = Date.now(), lastTick = Date.now(), IDLE = 60000;
    ['pointerdown', 'keydown', 'scroll', 'touchstart'].forEach(function (ev) {
      window.addEventListener(ev, function () { lastInput = Date.now(); }, { passive: true });
    });
    setInterval(function () {                       // count a second only while visible and recently used
      var now = Date.now(), dt = Math.min(now - lastTick, 2000); lastTick = now;
      if (document.visibilityState === 'visible' && now - lastInput < IDLE) active += dt;
    }, 1000);
    function beat(useBeacon) {
      var secs = Math.round(active / 1000); active -= secs * 1000;
      var body = JSON.stringify({ seconds: secs, anchor: currentAnchor() });
      if (useBeacon && navigator.sendBeacon) navigator.sendBeacon(base + '/lesson-time', new Blob([body], { type: 'application/json' }));
      else fetch(base + '/lesson-time', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: body }).catch(function () {});
    }
    setInterval(function () { beat(false); }, 20000);
    document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') beat(true); });
    window.addEventListener('pagehide', function () { beat(true); });
    setTimeout(function () { beat(false); }, 3000);   // records the start and the spot soon after opening
  }

  // ---------- Resume: land on the saved spot once images have settled
  if (/^#gsl-/.test(location.hash)) {
    var go = function () { var t = document.getElementById(location.hash.slice(1)); if (t) t.scrollIntoView(); };
    window.addEventListener('load', function () { go(); setTimeout(go, 400); });
  }
})();
