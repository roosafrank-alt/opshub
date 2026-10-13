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
