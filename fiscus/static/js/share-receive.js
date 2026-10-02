/*
 * Receiving a CSV from Android's share sheet. The service worker caught the
 * share and holds the file in memory; this asks for it once and drops it into
 * the file input. Nothing is uploaded until the person presses the button.
 */
(function () {
  'use strict';
  function take() {
    const input = document.getElementById('file');
    if (!input || !navigator.serviceWorker || !navigator.serviceWorker.controller) return;
    if (!new URLSearchParams(window.location.search).has('shared')) return;
    navigator.serviceWorker.addEventListener('message', (e) => {
      if (!e.data || e.data.type !== 'SHARED_FILE' || !e.data.file) return;
      const dt = new DataTransfer();
      dt.items.add(e.data.file);
      input.files = dt.files;
    }, { once: true });
    navigator.serviceWorker.controller.postMessage({ type: 'TAKE_SHARED_FILE' });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', take);
  else take();
})();
