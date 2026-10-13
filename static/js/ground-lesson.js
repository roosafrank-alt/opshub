// GS-LESSONS: quiz answers and "I've got this" ticks on a Ground School lesson plan.
// Ticks are kept in this browser only (localStorage), as a study aid.
(function () {
  var root = document.getElementById('gsl');
  if (!root) return;
  var code = root.getAttribute('data-code');
  function store(k, v) {
    try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; }
  }
  var boxes = root.querySelectorAll('.gsl-done input');
  function progress() {
    var n = 0;
    boxes.forEach(function (b) { if (b.checked) n++; });
    var el = document.getElementById('gsl-progress');
    if (el) el.textContent = n + ' of ' + boxes.length + ' done';
  }
  boxes.forEach(function (b) {
    var key = 'gsl:' + code + ':' + b.getAttribute('data-i');
    b.checked = store(key) === '1';
    b.addEventListener('change', function () { store(key, b.checked ? '1' : '0'); progress(); });
  });
  progress();
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
