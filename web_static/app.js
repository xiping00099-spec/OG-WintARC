/* app.js - interactions for WINTER ARC. Plain JavaScript, no libraries. Every feature degrades gracefully without it. */
(function () {
  'use strict';
  var d = document, root = d.documentElement;
  function $(s, c) { return (c || d).querySelector(s); }
  function $$(s, c) { return Array.prototype.slice.call((c || d).querySelectorAll(s)); }
  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode: ignore */ } },
    del: function (k) { try { localStorage.removeItem(k); } catch (e) { /* ignore */ } }
  };
  var reduced = function () { return root.getAttribute('data-motion') === 'reduce'; };
  root.classList.add('js-ready');

  /* ---------- toasts ---------- */
  function toast(msg) {
    var box = $('.toasts'); if (!box) return;
    var t = d.createElement('div'); t.className = 'toast'; t.textContent = msg; box.appendChild(t);
    setTimeout(function () { t.classList.add('out'); setTimeout(function () { t.remove(); }, 320); }, 3000);
  }

  /* ---------- theme ---------- */
  function applyTheme(t) {
    var eff = t === 'system' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : t;
    root.setAttribute('data-theme', eff === 'dark' ? 'dark' : 'light');
    var m = $('meta[name=theme-color]'); if (m) m.setAttribute('content', eff === 'dark' ? '#0b0b0d' : '#fbfbf9');
  }
  function setTheme(t, silent) {
    store.set('wa.theme', t);
    if (d.startViewTransition && !reduced()) { d.startViewTransition(function () { applyTheme(t); }); }
    else { root.classList.add('theme-anim'); applyTheme(t); setTimeout(function () { root.classList.remove('theme-anim'); }, 500); }
    syncSettings();
    if (!silent) toast(t === 'system' ? 'Theme follows your device' : (t === 'dark' ? 'Dark theme' : 'Light theme'));
  }
  var tb = $('.theme-btn');
  if (tb) tb.addEventListener('click', function () { setTheme(root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark'); });

  /* ---------- navigation ---------- */
  var mb = $('.menu-btn');
  function closeMenu() { root.classList.remove('menu-open'); if (mb) mb.setAttribute('aria-expanded', 'false'); }
  if (mb) mb.addEventListener('click', function () { var o = root.classList.toggle('menu-open'); mb.setAttribute('aria-expanded', String(o)); });
  addEventListener('resize', function () { if (innerWidth > 1020) closeMenu(); });
  var nav = $('.nav'), ind = $('.nav-ind'), cur = $('.nav a[aria-current=page]');
  function place(a) {
    if (!nav || !ind) return;
    if (!a || getComputedStyle(ind).display === 'none') { ind.style.opacity = 0; return; }
    var nr = nav.getBoundingClientRect(), r = a.getBoundingClientRect();
    ind.style.width = r.width + 'px'; ind.style.transform = 'translateX(' + (r.left - nr.left) + 'px)'; ind.style.opacity = 1;
  }
  if (nav) {
    place(cur); addEventListener('resize', function () { place(cur); });
    $$('a', nav).forEach(function (a) { a.addEventListener('pointerenter', function () { place(a); }); a.addEventListener('focus', function () { place(a); }); });
    nav.addEventListener('pointerleave', function () { place(cur); });
  }

  /* ---------- dropdowns (details.menu) ---------- */
  d.addEventListener('click', function (e) { $$('details.menu[open]').forEach(function (m) { if (!m.contains(e.target)) m.removeAttribute('open'); }); });
  d.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    $$('details.menu[open]').forEach(function (m) { m.removeAttribute('open'); });
    closeMenu();
  });

  /* ---------- scroll reveal, meters, charts, counters ---------- */
  function countUp(el) {
    var to = parseFloat(el.getAttribute('data-to')), dec = parseInt(el.getAttribute('data-dec') || '0', 10);
    if (isNaN(to) || reduced()) return;
    var t0 = null, dur = 1100; el.textContent = (0).toFixed(dec);
    (function step(ts) {
      if (t0 === null) t0 = ts;
      var p = Math.min(1, (ts - t0) / dur), e = 1 - Math.pow(1 - p, 4);
      el.textContent = (to * e).toFixed(dec);
      if (p < 1) requestAnimationFrame(step); else el.textContent = to.toFixed(dec);
    })(performance.now());
  }
  $$('.stagger').forEach(function (p) {
    Array.prototype.forEach.call(p.children, function (c, i) { c.classList.add('reveal'); c.style.setProperty('--i', Math.min(i, 10)); });
  });
  var targets = $$('.reveal, .meter, .chart, .count');
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) {
        if (!e.isIntersecting) return;
        e.target.classList.add('in'); if (e.target.classList.contains('count')) countUp(e.target);
        io.unobserve(e.target);
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -6% 0px' });
    targets.forEach(function (t) { io.observe(t); });
  } else { targets.forEach(function (t) { t.classList.add('in'); }); }

  /* ---------- tooltips (charts) ---------- */
  var tip = d.createElement('div'); tip.className = 'tip'; tip.setAttribute('role', 'tooltip'); d.body.appendChild(tip);
  var tipTimer;
  function showTip(el) {
    var txt = el.getAttribute('data-tip'); if (!txt) return;
    tip.textContent = txt; var r = el.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
    var x = Math.max(8, Math.min(innerWidth - w - 8, r.left + r.width / 2 - w / 2)), y = r.top - h - 10; if (y < 8) y = r.bottom + 10;
    tip.style.left = x + 'px'; tip.style.top = y + 'px'; tip.classList.add('on');
  }
  function hideTip() { tip.classList.remove('on'); }
  d.addEventListener('pointerover', function (e) { var t = e.target.closest && e.target.closest('[data-tip]'); if (t) showTip(t); });
  d.addEventListener('pointerout', function (e) { if (e.target.closest && e.target.closest('[data-tip]')) hideTip(); });
  d.addEventListener('focusin', function (e) { var t = e.target.closest && e.target.closest('[data-tip]'); if (t) showTip(t); });
  d.addEventListener('focusout', hideTip);
  d.addEventListener('pointerdown', function (e) {
    var t = e.target.closest && e.target.closest('[data-tip]');
    if (t && e.pointerType === 'touch') { showTip(t); clearTimeout(tipTimer); tipTimer = setTimeout(hideTip, 2200); }
  });
  addEventListener('scroll', hideTip, { passive: true });

  /* ---------- card spotlight (mouse only) ---------- */
  if (matchMedia('(pointer: fine)').matches) {
    var raf = 0;
    d.addEventListener('pointermove', function (e) {
      var c = e.target.closest && e.target.closest('.card.interactive'); if (!c || raf) return;
      raf = requestAnimationFrame(function () { raf = 0; var r = c.getBoundingClientRect(); c.style.setProperty('--mx', (e.clientX - r.left) + 'px'); c.style.setProperty('--my', (e.clientY - r.top) + 'px'); });
    });
  }

  /* ---------- confirmation dialog + loading buttons ---------- */
  var dlg = $('#confirm');
  function ask(title, msg, label) {
    return new Promise(function (resolve) {
      if (!dlg || !dlg.showModal) { resolve(window.confirm(title + '\n' + msg)); return; }
      $('h2', dlg).textContent = title; $('p', dlg).textContent = msg; $('[value=ok]', dlg).textContent = label || 'Confirm';
      dlg.returnValue = ''; dlg.showModal();
      dlg.addEventListener('close', function once() { dlg.removeEventListener('close', once); resolve(dlg.returnValue === 'ok'); });
    });
  }
  d.addEventListener('click', function (e) {
    var el = e.target.closest && e.target.closest('[data-confirm]'); if (!el) return;
    if (el.getAttribute('data-ok') === '1') { el.removeAttribute('data-ok'); return; }
    e.preventDefault();
    ask(el.getAttribute('data-confirm-title') || 'Are you sure?', el.getAttribute('data-confirm'), el.getAttribute('data-confirm-label')).then(function (ok) {
      if (ok) { el.setAttribute('data-ok', '1'); el.click(); }
    });
  });
  d.addEventListener('submit', function (e) {
    var b = e.submitter || $('button[type=submit], button:not([type])', e.target);
    if (b && !e.defaultPrevented && !e.target.hasAttribute('data-noload')) b.classList.add('is-loading');
  });
  var busyTimer;
  d.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href]');
    if (!a || e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || a.target === '_blank' || a.hasAttribute('download') || a.hasAttribute('data-confirm')) return;
    var u = new URL(a.href, location.href); if (u.origin !== location.origin || (u.pathname === location.pathname && u.search === location.search) || /\.xlsx$/.test(u.pathname)) return;
    closeMenu(); clearTimeout(busyTimer); busyTimer = setTimeout(function () { root.classList.add('is-busy'); }, 450);
  });
  addEventListener('pageshow', function () { clearTimeout(busyTimer); root.classList.remove('is-busy'); $$('.is-loading').forEach(function (b) { b.classList.remove('is-loading'); }); });

  /* ---------- calendar ---------- */
  function showDay(btn) {
    $$('.day[aria-pressed=true]').forEach(function (b) { b.setAttribute('aria-pressed', 'false'); });
    btn.setAttribute('aria-pressed', 'true');
    var p = $('#day-panel'); if (!p) return;
    var ds = btn.dataset, rows = [['Status', ds.status], ['Verified study', ds.study], ['Report', ds.report], ['Points', ds.points], ['Penalty', ds.penalty]].filter(function (r) { return r[1]; });
    p.textContent = '';
    var h = d.createElement('h3'); h.textContent = ds.label; p.appendChild(h);
    var dl = d.createElement('dl');
    rows.forEach(function (r) { var dt = d.createElement('dt'), dd = d.createElement('dd'); dt.textContent = r[0]; dd.textContent = r[1]; dl.appendChild(dt); dl.appendChild(dd); });
    p.appendChild(dl); p.classList.remove('pop'); void p.offsetWidth; p.classList.add('pop');
  }
  d.addEventListener('click', function (e) { var b = e.target.closest && e.target.closest('.day[data-label]'); if (b) showDay(b); });
  $$('select[data-autosubmit]').forEach(function (s) { s.addEventListener('change', function () { s.form.requestSubmit ? s.form.requestSubmit() : s.form.submit(); }); });

  /* ---------- settings page ---------- */
  function syncSettings() {
    var theme = store.get('wa.theme') || 'light', intro = store.get('wa.intro') || 'first', motion = store.get('wa.motion') || 'system';
    [['theme', theme], ['intro', intro], ['motion', motion]].forEach(function (pair) {
      $$('[data-setting=' + pair[0] + '] [data-value]').forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-value') === pair[1])); });
    });
  }
  $$('[data-setting] [data-value]').forEach(function (b) {
    b.addEventListener('click', function () {
      var key = b.parentNode.getAttribute('data-setting'), v = b.getAttribute('data-value');
      if (key === 'theme') return setTheme(v);
      if (key === 'intro') { store.set('wa.intro', v); toast(v === 'never' ? 'Opening scene turned off' : v === 'always' ? 'Opening scene shows on every visit' : 'Opening scene shows on your first visit only'); }
      if (key === 'motion') {
        if (v === 'reduce') { store.set('wa.motion', 'reduce'); root.setAttribute('data-motion', 'reduce'); }
        else { store.del('wa.motion'); if (!matchMedia('(prefers-reduced-motion: reduce)').matches) root.removeAttribute('data-motion'); }
        toast(v === 'reduce' ? 'Animations reduced' : 'Animations follow your device');
      }
      syncSettings();
    });
  });
  syncSettings();

  /* ---------- opening scene: The Study Sanctuary ---------- */
  var intro = $('#intro'), autoTimer, lastFocus;
  function inertPage(on) { $$('.site-header, main, .site-footer').forEach(function (el) { if (on) el.setAttribute('inert', ''); else el.removeAttribute('inert'); }); }
  function finishIntro() {
    if (!intro || intro.classList.contains('leaving')) return;
    clearTimeout(autoTimer); intro.classList.add('leaving'); root.classList.remove('intro-pending'); inertPage(false);
    store.set('wa.introSeen', '1');
    setTimeout(function () { intro.hidden = true; var m = $('#main'); if (m) m.focus({ preventScroll: true }); }, reduced() ? 20 : 950);
  }
  function pullCord() {
    var cord = $('.cord', intro); if (!intro || intro.classList.contains('lit') || cord.classList.contains('pulled')) return;
    cord.classList.remove('idle'); cord.classList.add('pulled');
    setTimeout(function () {
      intro.classList.add('lit'); cord.setAttribute('aria-label', 'Lamp is on');
      var live = $('.intro-live', intro); if (live) live.textContent = 'The lamp is on. Welcome to Winter Arc.';
      autoTimer = setTimeout(finishIntro, reduced() ? 1200 : 3600);
    }, reduced() ? 0 : 280);
  }
  function playIntro() {
    if (!intro) return;
    intro.hidden = false; intro.classList.remove('lit', 'leaving'); var cord = $('.cord', intro); cord.classList.remove('pulled'); cord.classList.add('idle');
    cord.setAttribute('aria-label', 'Pull the cord to turn on the lamp');
    $$('.intro-copy .quote, .intro-copy .hint, .intro-actions', intro).forEach(function (el) { el.style.animation = 'none'; void el.offsetWidth; el.style.animation = ''; });
    lastFocus = d.activeElement; root.classList.add('intro-pending'); inertPage(true);
    setTimeout(function () { try { cord.focus({ preventScroll: true }); } catch (e) { /* ignore */ } }, 60);
  }
  if (intro) {
    var cord = $('.cord', intro);
    cord.addEventListener('click', pullCord);
    cord.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pullCord(); } });
    var sy = null;
    cord.addEventListener('pointerdown', function (e) { sy = e.clientY; try { cord.setPointerCapture(e.pointerId); } catch (x) { /* ignore */ } });
    cord.addEventListener('pointermove', function (e) { if (sy !== null && e.clientY - sy > 14) { sy = null; pullCord(); } });
    cord.addEventListener('pointerup', function () { sy = null; });
    $('[data-intro-enter]', intro).addEventListener('click', finishIntro);
    $('[data-intro-skip]', intro).addEventListener('click', finishIntro);
    intro.addEventListener('keydown', function (e) { if (e.key === 'Escape') finishIntro(); });
    var replay = $('#replay-intro'); if (replay) replay.addEventListener('click', playIntro);
    if (root.classList.contains('intro-pending')) {
      intro.hidden = false; inertPage(true); store.set('wa.introSeen', '1');  /* counts as seen the moment it appears */
      setTimeout(function () { try { cord.focus({ preventScroll: true }); } catch (e) { /* ignore */ } }, 80);
    }
  }
  /* page-load focus + a safety net so a script error never leaves the page hidden */
  addEventListener('error', function () { root.classList.remove('intro-pending'); if (intro) { intro.hidden = true; } inertPage(false); });
})();
