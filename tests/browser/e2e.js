process.env.AWS_EXECUTION_ENV = 'AWS_Lambda_nodejs20.x';
const chromium = require('@sparticuz/chromium').default; const puppeteer = require('puppeteer-core');
const BASE = process.env.BASE_URL || 'http://127.0.0.1:18090', SH = (process.env.SHOTS_DIR || './shots') + '/';
require('fs').mkdirSync(SH, { recursive: true });
const results = []; const ok = (name, cond, extra = '') => { results.push([cond ? 'PASS' : 'FAIL', name, extra]); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const exe = await chromium.executablePath();
  const browser = await puppeteer.launch({ args: [...chromium.args.filter(a => a !== '--single-process' && a !== '--no-zygote'), '--no-sandbox', '--disable-dev-shm-usage'], executablePath: exe, headless: 'shell' });
  async function ctxPage(opts = {}) {
    const ctx = await browser.createBrowserContext(); const page = await ctx.newPage();
    page.errors = []; page.bad = [];
    page.on('pageerror', e => page.errors.push('pageerror: ' + e.message));
    page.on('console', m => { if (m.type() === 'error') page.errors.push('console: ' + m.text()); });
    page.on('response', r => { if (r.status() >= 400 && !r.url().endsWith('/favicon.ico')) page.bad.push(r.status() + ' ' + r.url()); });
    page.on('requestfailed', r => page.bad.push('failed ' + r.url()));
    await page.setViewport(opts.viewport || { width: 1280, height: 800 });
    if (opts.reduced) await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'reduce' }]);
    return page;
  }
  const cls = (page, sel) => page.$eval(sel, e => e.getAttribute('class') || '');
  const visible = (page, sel) => page.$eval(sel, e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity > 0.05; }).catch(() => false);

  // 1. first visit: intro shows, cord pull lights lamp, brand reveals, enters dashboard, remembered
  let p = await ctxPage();
  await p.goto(BASE + '/', { waitUntil: 'networkidle0' });
  ok('intro visible on first visit', await visible(p, '#intro'));
  ok('dashboard is inert behind intro', await p.$eval('main', e => e.hasAttribute('inert')));
  await p.waitForFunction(() => +getComputedStyle(document.querySelector('.intro-copy .hint')).opacity > 0.9, { timeout: 8000 });
  await p.screenshot({ path: SH + '01-intro-off.png' });
  ok('quote shown', (await p.$eval('.intro-copy .quote', e => e.textContent)).includes('single decision'));
  ok('hint shown', await visible(p, '.intro-copy .hint'));
  await p.click('.cord .hit');
  await p.waitForFunction(() => document.getElementById('intro').classList.contains('lit'), { timeout: 4000 });
  ok('cord pulled class', (await cls(p, '.cord')).includes('pulled'));
  ok('lamp lit', true);
  await p.waitForFunction(() => +getComputedStyle(document.querySelector('.intro-brand')).opacity > 0.95, { timeout: 4000 });
  ok('brand revealed', await visible(p, '.intro-brand .b1'));
  const bt = await p.$eval('.intro-brand', e => e.textContent);
  ok('brand shows session info', bt.includes('Session 02') && bt.includes('1 November 2026'), bt);
  await p.click('[data-intro-enter]');
  await p.waitForFunction(() => document.getElementById('intro').hidden, { timeout: 4000 });
  ok('intro gone after enter', !(await visible(p, '#intro')));
  ok('page no longer inert', !(await p.$eval('main', e => e.hasAttribute('inert'))));
  ok('seen flag stored', (await p.evaluate(() => localStorage.getItem('wa.introSeen'))) === '1');
  await sleep(1200); await p.screenshot({ path: SH + '03-home-light.png' });
  ok('doc welcome visible', await visible(p, '.hero .doctor'));
  await p.reload({ waitUntil: 'networkidle0' });
  ok('intro NOT shown on 2nd visit', !(await visible(p, '#intro')));
  ok('no JS errors (intro flow)', p.errors.length === 0, p.errors.join(' | '));
  ok('no failed requests (intro flow)', p.bad.length === 0, p.bad.join(' | '));
  let auto = await ctxPage(); await auto.goto(BASE + '/', { waitUntil: 'networkidle0' }); await auto.click('.cord .hit');
  await auto.waitForFunction(() => document.getElementById('intro').hidden, { timeout: 12000 }).catch(() => {});
  ok('intro continues into dashboard by itself after lamp is on', await auto.$eval('#intro', e => e.hidden));

  // 2. keyboard-only: Tab to cord, Enter lights, Escape skips
  let k = await ctxPage(); await k.goto(BASE + '/', { waitUntil: 'networkidle0' }); await sleep(300);
  ok('cord focused automatically', await k.evaluate(() => document.activeElement && document.activeElement.classList.contains('cord')));
  await k.keyboard.press('Enter'); await sleep(900);
  ok('keyboard Enter lights lamp', (await cls(k, '#intro')).includes('lit'));
  await k.keyboard.press('Escape'); await sleep(1300);
  ok('Escape closes intro', !(await visible(k, '#intro')));
  // 3. skip button
  let s = await ctxPage(); await s.goto(BASE + '/', { waitUntil: 'networkidle0' }); await sleep(300);
  await s.click('[data-intro-skip]'); await sleep(1300);
  ok('Skip intro works without lighting lamp', !(await visible(s, '#intro')) && (await s.evaluate(() => localStorage.getItem('wa.introSeen'))) === '1');
  // 4. reduced motion: still usable, lamp lights fast
  let r = await ctxPage({ reduced: true }); await r.goto(BASE + '/', { waitUntil: 'networkidle0' }); await sleep(200);
  ok('reduced-motion attribute set', (await r.$eval('html', e => e.getAttribute('data-motion'))) === 'reduce');
  await r.click('.cord .hit'); await sleep(400);
  ok('reduced motion lamp lights quickly', (await cls(r, '#intro')).includes('lit'));
  await sleep(1800); ok('reduced motion intro auto-continues', !(await visible(r, '#intro')));

  // 5. theme toggle persists
  p = await ctxPage(); await p.goto(BASE + '/', { waitUntil: 'networkidle0' }); await p.click('[data-intro-skip]'); await sleep(1200);
  ok('default theme light', (await p.$eval('html', e => e.dataset.theme)) === 'light');
  await p.click('.theme-btn'); await sleep(700);
  ok('toggle -> dark', (await p.$eval('html', e => e.dataset.theme)) === 'dark');
  await p.screenshot({ path: SH + '04-home-dark.png' });
  await p.reload({ waitUntil: 'networkidle0' });
  ok('dark persists after reload', (await p.$eval('html', e => e.dataset.theme)) === 'dark');
  const bg = await p.$eval('body', e => getComputedStyle(e).backgroundColor); ok('dark background applied', bg === 'rgb(11, 11, 13)', bg);

  // 6. every page loads w/o errors, counters finish, nothing overflows (desktop)
  const pages = ['/', '/calendar?p=2', '/leaderboard', '/leaderboard?kind=streak', '/streaks', '/stats', '/participants', '/participant/2', '/about', '/settings', '/admin/login', '/nope'];
  await p.evaluate(() => localStorage.setItem('wa.theme', 'light'));
  let i = 0;
  for (const path of pages) {
    const q = await ctxPage(); await q.evaluate(() => 0);
    await q.goto(BASE + '/settings'); await q.evaluate(() => { localStorage.setItem('wa.introSeen', '1'); });
    await q.goto(BASE + path, { waitUntil: 'networkidle0' }); await sleep(500);
    await q.evaluate(async () => { for (let y = 0; y < document.body.scrollHeight; y += 500) { window.scrollTo({ top: y, behavior: 'instant' }); await new Promise(r => setTimeout(r, 90)); } window.scrollTo({ top: 0, behavior: 'instant' }); });
    await sleep(1500);
    const over = await q.evaluate(() => document.documentElement.scrollWidth - innerWidth);
    const errs = q.errors.filter(x => !(path.includes('nope') && x.includes('404'))); ok(`page ${path}: no JS errors`, errs.length === 0, errs.join(' | '));
    ok(`page ${path}: no failed requests`, q.bad.filter(b => !path.includes('nope') || !b.includes('/nope')).length === 0, q.bad.join(' | '));
    ok(`page ${path}: no horizontal overflow (desktop)`, over <= 1, 'overflow=' + over);
    const unfinished = await q.evaluate(() => [...document.querySelectorAll('.count')].filter(e => Math.abs(parseFloat(e.textContent) - parseFloat(e.dataset.to)) > 0.001).length);
    ok(`page ${path}: counters reach real values`, unfinished === 0, 'unfinished=' + unfinished);
    const hidden = await q.evaluate(() => [...document.querySelectorAll('.reveal')].filter(e => +getComputedStyle(e).opacity < 0.9).length);
    ok(`page ${path}: scroll reveals all completed`, hidden === 0, 'hidden=' + hidden);
    await q.screenshot({ path: SH + `10-desktop-${String(++i).padStart(2, '0')}-${path.replace(/[^a-z0-9]/gi, '_')}.png`, fullPage: true });
    await q.close();
  }

  // 6b. creator name renders with bundled glyphs
  p = await ctxPage(); await p.goto(BASE + '/about', { waitUntil: 'networkidle0' }); await sleep(800);
  ok('creator name shown exactly (plain A)', (await p.$eval('.about-hero .name', n => n.textContent)) === 'Aʀcᴀɴᴇ ( OG ASSOCIATION )', await p.$eval('.about-hero .name', n => n.textContent));
  ok('footer credit shows same name', (await p.$eval('.site-footer .creator-name', n => n.textContent)) === 'Aʀcᴀɴᴇ ( OG ASSOCIATION )');
  ok('creator link points to Telegram', (await p.$eval('.handle', a => a.href)) === 'https://t.me/TalkToArcane' && (await p.$eval('.handle', a => a.target)) === '_blank');
  await p.screenshot({ path: SH + '11-about.png' });
  // 7. calendar day details + chart tooltip
  p = await ctxPage(); await p.goto(BASE + '/settings'); await p.evaluate(() => localStorage.setItem('wa.introSeen', '1'));
  await p.goto(BASE + '/calendar?p=2', { waitUntil: 'networkidle0' }); await sleep(800);
  await p.click('.day.GREEN'); await sleep(400);
  const panel = await p.$eval('#day-panel', e => e.innerText); ok('calendar day panel fills', /Status/.test(panel) && /Full green/.test(panel) && /Points/.test(panel), panel.replace(/\n/g, ' / '));
  ok('selected day marked', (await p.$$('.day[aria-pressed=true]')).length === 1);
  await p.goto(BASE + '/stats', { waitUntil: 'networkidle0' }); await sleep(1400);
  await p.evaluate(() => document.querySelector('.chart .cbar:not(.zero)').scrollIntoView({ behavior: 'instant', block: 'center' })); await sleep(1600); const geo = await p.evaluate(() => [...document.querySelectorAll('.chart .cbar')].map(b => { const bb = b.getBBox(); return { h: bb.height, attr: +b.getAttribute('height'), bottom: bb.y + bb.height, zero: b.classList.contains('zero') }; }));
  const base = geo.find(g => !g.zero).bottom;
  ok('bar heights match data (no CSS override)', geo.every(g => Math.abs(g.h - g.attr) < 0.6), JSON.stringify(geo.filter(g => Math.abs(g.h - g.attr) >= 0.6).slice(0, 2)));
  ok('all bars sit on the same baseline', geo.filter(g => !g.zero).every(g => Math.abs(g.bottom - base) < 0.6));
  const bar = await p.$('.chart .cbar:not(.zero)'); await bar.hover(); await sleep(400);
  ok('chart tooltip shows real value', (await p.$eval('.tip', e => e.classList.contains('on') && /\d+\.\d h/.test(e.textContent))), await p.$eval('.tip', e => e.textContent));
  await p.screenshot({ path: SH + '05-stats-tooltip.png' });
  const ch = await p.$('.chart'); await p.setViewport({ width: 1280, height: 800, deviceScaleFactor: 3 }); await ch.screenshot({ path: SH + '12-chart-zoom.png' });

  // 8. settings: never-intro, replay intro, motion
  await p.goto(BASE + '/settings', { waitUntil: 'networkidle0' });
  await p.click('[data-setting=intro] [data-value=always]'); await sleep(300);
  ok('toast appears', await visible(p, '.toast'));
  await p.goto(BASE + '/', { waitUntil: 'networkidle0' }); ok('intro on every visit when chosen', await visible(p, '#intro'));
  await p.click('[data-intro-skip]'); await sleep(1200);
  await p.goto(BASE + '/settings', { waitUntil: 'networkidle0' }); await p.click('[data-setting=intro] [data-value=never]');
  await p.goto(BASE + '/', { waitUntil: 'networkidle0' }); ok('intro hidden when set to never', !(await visible(p, '#intro')));
  await p.goto(BASE + '/settings', { waitUntil: 'networkidle0' }); await p.click('#replay-intro'); await sleep(600);
  ok('Replay intro works from Settings', await visible(p, '#intro')); await p.click('[data-intro-skip]'); await sleep(1200);

  // 9. admin: login, dashboard, confirm dialogs
  await p.goto(BASE + '/admin/login', { waitUntil: 'networkidle0' });
  await p.type('#pw', 'wrong-pass'); await p.click('button[type=submit]'); await sleep(800);
  ok('wrong password shows error state', (await p.$eval('.notice.bad', n => n.textContent)).includes('password') && await visible(p, '.doctor[data-state=error]'));
  await p.screenshot({ path: SH + '06-admin-login-error.png' });
  await p.type('#pw', 'demo-password-123'); await p.click('button[type=submit]'); await p.waitForNavigation({ waitUntil: 'networkidle0' }).catch(() => {}); await sleep(800);
  ok('admin dashboard after login', (await p.content()).includes('Control room'));
  await p.screenshot({ path: SH + '07-admin-dashboard.png', fullPage: true });
  await p.click('form[action="/admin/logout"] button'); await sleep(500);
  ok('logout asks for confirmation (dialog open)', await p.$eval('#confirm', d => d.open));
  await p.screenshot({ path: SH + '08-confirm-dialog.png' });
  await p.click('#confirm button[value=cancel]'); await sleep(500);
  ok('cancel keeps admin session', (await p.url()).endsWith('/admin') && (await p.content()).includes('Control room'));
  await p.click('form[action="/admin/logout"] button'); await sleep(400); await p.click('#confirm button[value=ok]'); await sleep(1200);
  ok('confirm logs out', !(await p.content()).includes('Control room'));
  await p.goto(BASE + '/admin'); ok('admin protected after logout', (await p.url()).includes('/admin/login'));

  // 10. mobile phone
  const m = await ctxPage({ viewport: { width: 390, height: 844, isMobile: true, hasTouch: true, deviceScaleFactor: 2 } });
  await m.goto(BASE + '/', { waitUntil: 'networkidle0' }); await sleep(2800);
  await m.screenshot({ path: SH + '20-mobile-intro-off.png' });
  const hit = await (await m.$('.cord .hit')).boundingBox(); ok('cord touch target >= 44px', hit.width >= 44 && hit.height >= 44, JSON.stringify(hit));
  await m.tap('.cord .hit'); await sleep(1800); await m.screenshot({ path: SH + '21-mobile-intro-lit.png' });
  ok('mobile: tap lights lamp', (await cls(m, '#intro')).includes('lit'));
  if (!(await m.$eval('#intro', n => n.hidden))) { await m.tap('[data-intro-enter]').catch(() => {}); } await m.waitForFunction(() => document.getElementById('intro').hidden, { timeout: 6000 }); await sleep(800);
  await m.screenshot({ path: SH + '22-mobile-home.png', fullPage: true });
  await m.tap('.menu-btn'); await sleep(700); await m.screenshot({ path: SH + '23-mobile-menu.png' });
  ok('mobile menu opens', await m.evaluate(() => document.documentElement.classList.contains('menu-open')) && await visible(m, '.nav a'));
  const sheet = await m.$eval('.nav', n => { const r = n.getBoundingClientRect(); const links = [...n.querySelectorAll('a')].filter(a => { const b = a.getBoundingClientRect(); return b.height > 20 && b.top >= 0 && b.bottom <= innerHeight && +getComputedStyle(a).opacity > .9; }).length; return { w: r.width, h: r.height, vw: innerWidth, vh: innerHeight, top: r.top, links }; });
  ok('mobile menu sheet covers the screen with all 8 links visible', sheet.w >= sheet.vw - 1 && sheet.h >= sheet.vh - 70 && sheet.links === 8, JSON.stringify(sheet));
  ok('menu button aria-expanded true', (await m.$eval('.menu-btn', e => e.getAttribute('aria-expanded'))) === 'true');
  await m.keyboard.press('Escape'); await sleep(400);
  ok('Escape closes mobile menu', !(await m.evaluate(() => document.documentElement.classList.contains('menu-open'))));
  for (const path of ['/', '/calendar?p=2', '/leaderboard', '/streaks', '/stats', '/participants', '/participant/2', '/about', '/settings', '/admin/login']) {
    await m.goto(BASE + path, { waitUntil: 'networkidle0' }); await sleep(900);
    await m.evaluate(async () => { for (let y = 0; y < document.body.scrollHeight; y += 400) { window.scrollTo({ top: y, behavior: 'instant' }); await new Promise(r => setTimeout(r, 90)); } window.scrollTo({ top: 0, behavior: 'instant' }); });
    await sleep(1200);
    const over = await m.evaluate(() => document.documentElement.scrollWidth - innerWidth);
    ok(`mobile ${path}: no horizontal overflow`, over <= 1, 'overflow=' + over);
    const small = await m.evaluate(() => [...document.querySelectorAll('a.btn,button.btn,.icon-btn,.seg a,.seg button')].filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && (r.height < 34 || r.width < 34); }).length);
    ok(`mobile ${path}: touch targets reasonable`, small === 0, 'small=' + small);
    await m.screenshot({ path: SH + `30-mobile-${path.replace(/[^a-z0-9]/gi, '_')}.png`, fullPage: true });
  }
  ok('no JS errors on mobile run', m.errors.length === 0, m.errors.join(' | '));

  await browser.close();
  const fails = results.filter(r => r[0] === 'FAIL');
  for (const [s, n, e] of results) console.log(s, n, e && s === 'FAIL' ? '→ ' + e : '');
  console.log(`\n${results.length - fails.length}/${results.length} browser checks passed`);
})().catch(e => { for (const [s, n, x] of results) console.log(s, n, s === 'FAIL' ? '→ ' + x : ''); console.error('SCRIPT FAILED after', results.length, 'checks:', e.stack.split('\n').slice(0, 4).join('\n')); process.exit(1); });
