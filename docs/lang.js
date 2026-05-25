document.addEventListener('click', function (e) {
  var link = e.target.closest && e.target.closest('.lang-switcher a[data-lang]');
  if (!link) return;
  try { localStorage.setItem('lang', link.getAttribute('data-lang')); }
  catch (_) { /* localStorage disabled — ignore */ }
});
