/* Lightweight guided-tour engine: a dimmed overlay with a "spotlight" cut
   out around one nav element at a time, a popover with an arrow pointing
   at it, and a Next/Skip/Got it flow the person clicks through. No
   dependencies (no library) - just enough to walk a new user through the
   handful of things worth knowing on first login, and replayable later
   from My Account. */
window.OpsHubTour = (function () {
  function start(steps, opts) {
    opts = opts || {};
    steps = (steps || []).filter(function (s) { return !s.selector || document.querySelector(s.selector); });
    if (!steps.length) { if (opts.onFinish) opts.onFinish(); return; }

    var idx = 0;
    var spotlight, popover, arrow;

    function el(tag, css) {
      var e = document.createElement(tag);
      e.style.cssText = css;
      document.body.appendChild(e);
      return e;
    }

    function cleanup() {
      [spotlight, popover].forEach(function (e) { if (e && e.parentNode) e.parentNode.removeChild(e); });
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('resize', reposition);
    }
    function onKey(e) { if (e.key === 'Escape') finish(); }
    function finish() {
      cleanup();
      if (opts.onFinish) opts.onFinish();
    }

    function reposition() {
      var step = steps[idx];
      var target = step.selector ? document.querySelector(step.selector) : null;

      if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        var r = target.getBoundingClientRect();
        spotlight.style.display = '';
        spotlight.style.left = (r.left - 6) + 'px';
        spotlight.style.top = (r.top - 6) + 'px';
        spotlight.style.width = Math.max(0, r.width + 12) + 'px';
        spotlight.style.height = Math.max(0, r.height + 12) + 'px';
      } else {
        spotlight.style.display = 'none';
      }

      requestAnimationFrame(function () {
        var pr = popover.getBoundingClientRect();
        var top, left, arrowCss = 'display:none;';
        if (target) {
          var r2 = target.getBoundingClientRect();
          top = r2.bottom + 18;
          var pointDown = false;
          if (top + pr.height > window.innerHeight - 8) {
            top = Math.max(8, r2.top - pr.height - 18);
            pointDown = true;
          }
          left = Math.min(Math.max(8, r2.left), Math.max(8, window.innerWidth - pr.width - 8));
          var arrowLeft = Math.min(Math.max(14, (r2.left + r2.width / 2) - left - 8), pr.width - 30);
          arrowCss = 'display:block;left:' + arrowLeft + 'px;' +
            (pointDown
              ? 'bottom:-9px;border-width:9px 9px 0 9px;border-color:#fff transparent transparent transparent;'
              : 'top:-9px;border-width:0 9px 9px 9px;border-color:transparent transparent #fff transparent;');
        } else {
          top = (window.innerHeight - pr.height) / 2;
          left = (window.innerWidth - pr.width) / 2;
        }
        popover.style.top = top + 'px';
        popover.style.left = left + 'px';
        arrow.style.cssText = 'position:absolute;width:0;height:0;border-style:solid;' + arrowCss;
      });
    }

    function render() {
      var step = steps[idx];

      if (!spotlight) {
        spotlight = el('div', 'position:fixed;z-index:19001;border-radius:8px;pointer-events:none;' +
          'box-shadow:0 0 0 4px #ffc107, 0 0 0 9999px rgba(0,0,0,0.6);transition:left .25s ease,top .25s ease,width .25s ease,height .25s ease;');
      }
      if (!popover) {
        popover = el('div', 'position:fixed;z-index:19002;max-width:320px;background:#fff;color:#212529;' +
          'border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.4);padding:16px;font-family:inherit;');
        arrow = document.createElement('div');
        popover.appendChild(arrow);
      }

      var body = document.createElement('div');
      body.innerHTML =
        '<div class="small text-muted mb-1">Step ' + (idx + 1) + ' of ' + steps.length + '</div>' +
        '<h6 class="fw-bold mb-2">' + step.title + '</h6>' +
        '<p class="mb-3" style="font-size:.9rem;">' + step.text + '</p>' +
        '<div class="d-flex justify-content-between align-items-center">' +
        '<button type="button" class="btn btn-link btn-sm text-muted p-0" id="tour-skip-btn">Skip tour</button>' +
        '<button type="button" class="btn btn-primary btn-sm fw-bold" id="tour-next-btn">' +
        (idx === steps.length - 1 ? 'Got it' : 'Next') + '</button></div>';
      // Keep the arrow element, replace everything else.
      while (popover.lastChild) { popover.removeChild(popover.lastChild); }
      popover.appendChild(arrow);
      popover.appendChild(body);

      document.getElementById('tour-skip-btn').onclick = finish;
      document.getElementById('tour-next-btn').onclick = function () {
        idx++;
        if (idx >= steps.length) { finish(); } else { render(); }
      };

      reposition();
    }

    document.addEventListener('keydown', onKey);
    window.addEventListener('resize', reposition);
    render();
  }

  return { start: start };
})();
