// GS-LESSONS: quiz answers on a Ground School lesson plan.
(function () {
  var root = document.getElementById('gsl');
  if (!root) return;
  var right = 0, answered = 0, total = root.querySelectorAll('.gsl-q').length;
  root.querySelectorAll('.gsl-q').forEach(function (q) {
    var ans = parseInt(q.getAttribute('data-answer'), 10);
    q.querySelectorAll('button').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (q.getAttribute('data-done')) return;
        q.setAttribute('data-done', '1');
        answered++;
        var j = parseInt(btn.getAttribute('data-j'), 10);
        if (j === ans) right++;
        btn.classList.add(j === ans ? 'right' : 'wrong');
        var good = q.querySelector('button[data-j="' + ans + '"]');
        if (good) good.classList.add('right');
        q.querySelector('.gsl-qwhy').hidden = false;
        document.getElementById('gsl-score').textContent =
          right + ' of ' + answered + ' right' + (answered === total ? ' · done' : ' so far');
      });
    });
  });
})();

// GS-INTERACTIVE: games in the Play section (match, label, sort, order, true/false, flashcards).
(function () {
  var root = document.getElementById('gsl');
  if (!root) return;
  function shuffle(box) {
    var kids = Array.prototype.slice.call(box.children);
    for (var i = kids.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1)), t = kids[i]; kids[i] = kids[j]; kids[j] = t;
    }
    kids.forEach(function (k) { box.appendChild(k); });
  }
  function status(act, msg) { act.querySelector('.gsl-act-status').textContent = msg; }
  function shake(el) { el.classList.add('gsl-bad'); setTimeout(function () { el.classList.remove('gsl-bad'); }, 450); }

  root.querySelectorAll('.gsl-act').forEach(function (act) {
    act.querySelectorAll('[data-shuffle]').forEach(shuffle);
    var type = act.getAttribute('data-act');

    // match + label: tap one on each side; same data-k is a pair.
    var pairing = act.querySelector('.gsl-pairing');
    if (pairing) {
      var total = parseInt(pairing.getAttribute('data-total'), 10), got = 0, tries = 0, sel = null;
      pairing.querySelectorAll('[data-k]').forEach(function (el) {
        el.addEventListener('click', function () {
          if (el.disabled) return;
          if (!sel || sel.getAttribute('data-side') === el.getAttribute('data-side')) {
            if (sel) sel.classList.remove('gsl-sel');
            sel = el; el.classList.add('gsl-sel'); return;
          }
          tries++;
          if (sel.getAttribute('data-k') === el.getAttribute('data-k')) {
            [sel, el].forEach(function (x) { x.classList.remove('gsl-sel'); x.classList.add('gsl-ok'); x.disabled = true; });
            got++;
            status(act, got === total ? 'All matched in ' + tries + ' tries. Nice work!' : got + ' of ' + total + ' matched');
          } else {
            shake(sel); shake(el); sel.classList.remove('gsl-sel');
            status(act, 'Not that one. Try again.');
          }
          sel = null;
        });
      });
    }

    // sort: tap an item, then the bucket it belongs in.
    var sort = act.querySelector('.gsl-sort');
    if (sort) {
      var stotal = parseInt(sort.getAttribute('data-total'), 10), sdone = 0, ssel = null, miss = 0;
      sort.querySelectorAll('.gsl-item').forEach(function (it) {
        it.addEventListener('click', function () {
          if (ssel) ssel.classList.remove('gsl-sel');
          ssel = it; it.classList.add('gsl-sel');
        });
      });
      sort.querySelectorAll('.gsl-bucket').forEach(function (b) {
        b.addEventListener('click', function () {
          if (!ssel) { status(act, 'Tap an item first, then its bucket.'); return; }
          if (ssel.getAttribute('data-b') === b.getAttribute('data-b')) {
            var chip = document.createElement('span'); chip.className = 'gsl-placed'; chip.innerHTML = ssel.innerHTML;
            b.querySelector('.gsl-bucket-items').appendChild(chip);
            ssel.remove(); ssel = null; sdone++;
            status(act, sdone === stotal ? 'All sorted' + (miss ? ' (' + miss + ' misses)' : ' with no misses') + '!' : sdone + ' of ' + stotal + ' sorted');
          } else { miss++; shake(ssel); shake(b); status(act, 'Not that bucket. Try again.'); }
        });
      });
    }

    // order: move steps up/down, then check.
    var order = act.querySelector('.gsl-order');
    if (order) {
      order.addEventListener('click', function (e) {
        var btn = e.target.closest('button'); if (!btn) return;
        var li = btn.closest('li');
        if (btn.classList.contains('gsl-up') && li.previousElementSibling) order.insertBefore(li, li.previousElementSibling);
        if (btn.classList.contains('gsl-down') && li.nextElementSibling) order.insertBefore(li.nextElementSibling, li);
        order.querySelectorAll('li').forEach(function (x) { x.classList.remove('gsl-ok', 'gsl-bad'); });
      });
      act.querySelector('.gsl-order-check').addEventListener('click', function () {
        var right = 0, items = order.querySelectorAll('li');
        items.forEach(function (li, i) {
          var ok = parseInt(li.getAttribute('data-pos'), 10) === i;
          li.classList.toggle('gsl-ok', ok); li.classList.toggle('gsl-bad', !ok);
          if (ok) right++;
        });
        status(act, right === items.length ? 'Perfect order!' : right + ' of ' + items.length + ' in the right spot. Keep moving them.');
      });
    }

    // true / false rapid-fire with a streak.
    var tf = act.querySelector('.gsl-tf');
    if (tf) {
      var cards = tf.querySelectorAll('.gsl-tfcard'), ci = 0, streak = 0, best = 0, score = 0;
      var next = tf.querySelector('.gsl-tfnext');
      cards.forEach(function (c) {
        c.querySelectorAll('[data-v]').forEach(function (b) {
          b.addEventListener('click', function () {
            if (c.getAttribute('data-done')) return;
            c.setAttribute('data-done', '1');
            var ok = b.getAttribute('data-v') === c.getAttribute('data-a');
            b.classList.add(ok ? 'gsl-ok' : 'gsl-bad');
            if (ok) { score++; streak++; best = Math.max(best, streak); } else { streak = 0; }
            c.querySelector('.gsl-tfwhy').hidden = false;
            status(act, (ok ? 'Right! ' : 'Not quite. ') + 'Streak ' + streak + ' · ' + score + ' of ' + cards.length + ' right');
            if (ci < cards.length - 1) { next.hidden = false; }
            else { status(act, 'Done: ' + score + ' of ' + cards.length + ' right. Best streak ' + best + '.'); }
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
      function show(n) {
        fc[fi].hidden = true; fi = (n + fc.length) % fc.length; fc[fi].hidden = false;
        fc[fi].querySelector('.gsl-ffront').hidden = false; fc[fi].querySelector('.gsl-fback').hidden = true;
        fc[fi].classList.remove('gsl-flipped'); count.textContent = (fi + 1) + ' / ' + fc.length;
      }
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
})();
