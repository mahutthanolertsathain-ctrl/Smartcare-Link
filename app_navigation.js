(() => {
  // Only prefetch public page shells, never API data or patient-specific URLs.
  const pages = new Set(['/', '/screening', '/patient', '/community', '/nurse', '/dispatch', '/telemedicine', '/training']);
  const prefetched = new Set();
  const network = navigator.connection;
  if (network?.saveData || /(^|-)2g$/.test(network?.effectiveType || '')) return;
  function prepare(event) {
    const link = event.target.closest?.('a[href]');
    if (!link) return;
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin || url.search || !pages.has(url.pathname) || url.pathname === location.pathname || prefetched.has(url.pathname) || prefetched.size >= 2) return;
    prefetched.add(url.pathname);
    const hint = document.createElement('link');
    hint.rel = 'prefetch';
    hint.as = 'document';
    hint.href = url.href;
    document.head.appendChild(hint);
  }
  document.addEventListener('pointerover', prepare, {passive: true});
  document.addEventListener('focusin', prepare);
  document.addEventListener('touchstart', prepare, {passive: true});
})();
