/* LifeOS layer — YOUR skin over map.ca.
 *
 * Injected at serve time by mapca-local into every page of the real site.
 * The site is untouched on disk; this floats above it: a dock button that
 * opens your LifeOS (tools, profile, vault, prompt library) in the site's
 * own glass body-window, over whatever map.ca page you're on.
 * Esc or the button closes it. Nothing here phones anywhere but localhost.
 */
(function () {
  if (window.top !== window) return;            // never inside frames
  if (document.getElementById('lifeos-dock')) return;

  var btn = document.createElement('button');
  btn.id = 'lifeos-dock';
  btn.title = "Your LifeOS (Esc closes)";
  btn.textContent = 'M';
  btn.style.cssText =
    'position:fixed;right:22px;bottom:22px;z-index:2147483000;width:52px;height:52px;' +
    'border-radius:50%;border:0;cursor:pointer;background:#e31e24;color:#fff;' +
    'font:700 20px "IBM Plex Sans",system-ui,sans-serif;' +
    'box-shadow:0 8px 24px rgba(0,0,0,.28);transition:transform .12s';
  btn.onmouseenter = function () { btn.style.transform = 'scale(1.08)'; };
  btn.onmouseleave = function () { btn.style.transform = ''; };

  var wrap = null;
  function close() { if (wrap) { wrap.remove(); wrap = null; btn.textContent = 'M'; } }
  function open() {
    wrap = document.createElement('div');
    wrap.id = 'lifeos-layer';
    wrap.style.cssText =
      'position:fixed;inset:0;z-index:2147482999;display:flex;align-items:center;' +
      'justify-content:center;background:rgba(15,15,15,.25);backdrop-filter:blur(6px)';
    var win = document.createElement('div');   // the site's own body-window contract
    win.style.cssText =
      'width:min(1280px,calc(100vw - 2.25rem));height:min(calc(100vh - 6rem),900px);' +
      'border-radius:20px;overflow:hidden;border:1px solid rgba(255,255,255,.35);' +
      'box-shadow:0 8px 32px rgba(0,0,0,.15);background:rgba(255,255,255,.45);' +
      '-webkit-backdrop-filter:blur(28px);backdrop-filter:blur(28px)';
    var fr = document.createElement('iframe');
    fr.src = '/lifeos/lifeos.html';
    fr.style.cssText = 'width:100%;height:100%;border:0;display:block';
    win.appendChild(fr);
    wrap.appendChild(win);
    wrap.addEventListener('click', function (e) { if (e.target === wrap) close(); });
    document.body.appendChild(wrap);
    btn.textContent = '×';
  }
  btn.onclick = function () { wrap ? close() : open(); };
  addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
  document.body.appendChild(btn);
})();
