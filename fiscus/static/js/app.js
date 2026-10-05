/*
 * Fiscus app shell: service worker registration and the update prompt, the
 * offline banner, flash messages, and the event sheet the calendar opens.
 */
(function () {
  'use strict';

  function toast(message, actionLabel, onAction) {
    const old = document.querySelector('.toast');
    if (old) old.remove();
    const el = document.createElement('div');
    el.className = 'toast';
    el.setAttribute('role', 'status');
    const span = document.createElement('span');
    span.textContent = message;
    el.appendChild(span);
    if (actionLabel) {
      const btn = document.createElement('button');
      btn.className = 'btn btn-primary btn-sm';
      btn.textContent = actionLabel;
      btn.addEventListener('click', onAction);
      el.appendChild(btn);
    } else {
      setTimeout(() => el.remove(), 4000);
    }
    document.body.appendChild(el);
  }
  window.fiscusToast = toast;

  // ------------------------------------------------------- service worker
  if ('serviceWorker' in navigator) {
    // Reload only when the person accepted an update. The first install also
    // fires controllerchange (clients.claim), and reloading then would cut
    // across whatever the page was doing, such as finishing a passkey sign-up.
    let updateAccepted = false;
    let reloading = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (!updateAccepted || reloading) return;
      reloading = true;
      window.location.reload();
    });
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).then((reg) => {
        const offer = (worker) => {
          toast('A new version of Fiscus is ready.', 'Reload', () => {
            updateAccepted = true;
            worker.postMessage({ type: 'SKIP_WAITING' });
          });
        };
        if (reg.waiting && navigator.serviceWorker.controller) offer(reg.waiting);
        reg.addEventListener('updatefound', () => {
          const worker = reg.installing;
          if (!worker) return;
          worker.addEventListener('statechange', () => {
            if (worker.state === 'installed' && navigator.serviceWorker.controller) offer(worker);
          });
        });
        // Look for a new version when the app comes back to the foreground.
        document.addEventListener('visibilitychange', () => {
          if (document.visibilityState === 'visible') reg.update().catch(() => {});
        });
      }).catch(() => {});
    });
  }

  // --------------------------------------------------------------- offline
  function offlineBanner() {
    let el = document.getElementById('offline-banner');
    if (navigator.onLine) {
      if (el) el.remove();
      return;
    }
    if (!el) {
      el = document.createElement('div');
      el.id = 'offline-banner';
      el.className = 'banner-offline';
      el.textContent = 'You are offline. Figures will not update until you reconnect.';
      document.body.prepend(el);
    }
  }
  window.addEventListener('online', offlineBanner);
  window.addEventListener('offline', offlineBanner);
  document.addEventListener('DOMContentLoaded', offlineBanner);

  document.addEventListener('htmx:sendError', () => {
    // A boosted navigation that cannot reach the server: show the offline
    // screen rather than leave stale figures on the page.
    window.location.href = '/offline';
  });

  // ------------------------------------------------------------ the sheet
  function sheet() {
    let d = document.getElementById('sheet');
    if (!d) {
      d = document.createElement('dialog');
      d.id = 'sheet';
      d.className = 'sheet';
      d.innerHTML = '<div class="sheet-body"><button class="btn btn-sm close" type="button" data-close>Close</button><div id="sheet-content"></div></div>';
      d.addEventListener('click', (e) => {
        if (e.target === d || e.target.closest('[data-close]')) d.close();
      });
      document.body.appendChild(d);
    }
    return d;
  }

  document.addEventListener('click', (e) => {
    const trigger = e.target.closest('[data-sheet]');
    if (!trigger) return;
    e.preventDefault();
    const d = sheet();
    const content = d.querySelector('#sheet-content');
    content.textContent = 'Loading…';
    d.showModal();
    window.htmx.ajax('GET', trigger.getAttribute('data-sheet'), { target: '#sheet-content', swap: 'innerHTML' });
  });

  // --------------------------------------------------------- confirmations
  document.addEventListener('submit', (e) => {
    const form = e.target;
    const msg = form.getAttribute && form.getAttribute('data-confirm');
    if (msg && !window.confirm(msg)) {
      e.preventDefault();
      e.stopImmediatePropagation();
    }
  }, true);

  // ------------------------------------------------------------ copy links
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-copy]');
    if (!btn) return;
    const el = document.getElementById(btn.getAttribute('data-copy'));
    if (!el || !navigator.clipboard) return;
    navigator.clipboard.writeText(el.textContent.trim()).then(() => toast('Copied'));
  });

  // ------------------------------------------------- auto-submit selects
  document.addEventListener('change', (e) => {
    const el = e.target;
    if (el.matches && el.matches('select[data-autosubmit], input[data-autosubmit]')) {
      if (el.form) el.form.requestSubmit();
    }
  });

  // ------------------------------------- budget import: how often sets the amount
  // The file's amount is read at the chosen cycle: a yearly $2,960 changed to
  // every month is $2,960 a month. Back on the file's own cycle, its figure returns.
  // A typed amount becomes the base. Cents are whole numbers throughout.
  function cents(text) {
    const m = String(text).replace(/[$,\s]/g, '').match(/^(\d*)(?:\.(\d{0,2}))?$/);
    if (!m || (m[1] === '' && !m[2])) return null;
    return Number(m[1] || '0') * 100 + Number(((m[2] || '') + '00').slice(0, 2));
  }
  function plain(c) {
    return Math.floor(c / 100) + '.' + String(c % 100).padStart(2, '0');
  }
  const aud = new Intl.NumberFormat('en-AU', { style: 'currency', currency: 'AUD' });
  function savingHint(row, cycle, monthly) {
    const hint = row.querySelector('[data-saving-hint]');
    if (!hint) return;
    const sel = row.querySelector('select[data-cycle-select]');
    const label = sel.options[sel.selectedIndex].text.toLowerCase();
    hint.textContent = cycle > 1 && monthly !== null ? `Saving up ${aud.format((monthly * cycle) / 100)} ${label}` : '';
  }
  document.addEventListener('change', (e) => {
    const sel = e.target;
    if (!sel.matches || !sel.matches('select[data-cycle-select]')) return;
    const row = sel.closest('[data-budget-line]');
    const input = row.querySelector('input[data-monthly-input]');
    const cycle = Number(sel.value);
    const old = Number(row.dataset.cycle);
    let base = row.dataset.amount === '' ? null : Number(row.dataset.amount);
    if (base === null) {
      const typed = cents(input.value);
      base = typed === null ? null : typed * old;
    }
    let monthly = null;
    if (row.dataset.monthly !== '' && cycle === Number(row.dataset.fileCycle)) {
      monthly = Number(row.dataset.monthly);
    } else if (base !== null) {
      monthly = Math.round(base / cycle);
    }
    if (monthly !== null) input.value = plain(monthly);
    row.dataset.cycle = String(cycle);
    savingHint(row, cycle, monthly);
  });
  document.addEventListener('input', (e) => {
    const input = e.target;
    if (!input.matches || !input.matches('input[data-monthly-input]')) return;
    const row = input.closest('[data-budget-line]');
    const cycle = Number(row.dataset.cycle);
    const typed = cents(input.value);
    // From here on the typed figure leads, not the file's.
    row.dataset.amount = typed === null ? '' : String(typed * cycle);
    row.dataset.monthly = '';
    savingHint(row, cycle, typed);
  });

  // ------------------------------------------------- recategorise select all
  document.addEventListener('change', (e) => {
    if (!e.target.matches('[data-select-all]')) return;
    document.querySelectorAll('input[name="txn"]').forEach((cb) => { cb.checked = e.target.checked; });
  });

  // ------------------------------------------------ scenario form switcher
  function showKind(root) {
    root.querySelectorAll('select[data-kind-switch]').forEach((sel) => {
      const form = sel.closest('form');
      const apply = () => {
        form.querySelectorAll('[data-kind]').forEach((el) => {
          const on = el.getAttribute('data-kind').split(' ').includes(sel.value);
          el.classList.toggle('hidden', !on);
          el.querySelectorAll('input, select').forEach((i) => { i.disabled = !on; });
        });
      };
      sel.addEventListener('change', apply);
      apply();
    });
  }

  // ------------------------------------------------- bill split switcher
  function splitFields(root) {
    root.querySelectorAll('select[data-split-mode]').forEach((sel) => {
      const form = sel.closest('form');
      const apply = () => {
        const mode = sel.value;
        form.querySelectorAll('[data-split-input]').forEach((el) => {
          el.classList.toggle('hidden', mode === 'equal');
          const input = el.querySelector('input');
          if (input) input.placeholder = mode === 'percent' ? '%' : '$';
        });
      };
      sel.addEventListener('change', apply);
      apply();
    });
  }

  function init(root) {
    showKind(root);
    splitFields(root);
    const flash = root.querySelector && root.querySelector('[data-flash]');
    if (flash && flash.textContent.trim()) toast(flash.textContent.trim());
  }

  document.addEventListener('DOMContentLoaded', () => {
    init(document);
    if (window.htmx) window.htmx.onLoad((el) => { if (el !== document.body) init(el); else init(document); });
  });
})();
