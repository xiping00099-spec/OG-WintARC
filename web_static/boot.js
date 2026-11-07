/* boot.js - runs before first paint: theme, motion preference, and whether the opening scene should show. */
(function () {
  var d = document.documentElement;
  function get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  d.classList.add('js');
  var theme = get('wa.theme') || 'light';           /* light is the default */
  if (theme === 'system') theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  d.setAttribute('data-theme', theme === 'dark' ? 'dark' : 'light');
  if (matchMedia('(prefers-reduced-motion: reduce)').matches || get('wa.motion') === 'reduce') d.setAttribute('data-motion', 'reduce');
  var mode = get('wa.intro') || 'first';             /* first | always | never */
  if (location.pathname === '/' && mode !== 'never' && (mode === 'always' || get('wa.introSeen') !== '1')) d.classList.add('intro-pending');
})();
