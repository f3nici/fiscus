/*
 * Fiscus app shell: service worker registration and the update prompt, the
 * offline banner, flash messages, moving between screens and the menu, and
 * the event sheet the calendar opens.
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

  // ---------------------------------------------------- between screens
  // A tapped tab lights up at once, a bar runs along the top until the page
  // comes, and the page slides in from the side its tab is on (D67).
  const still = window.matchMedia('(prefers-reduced-motion: reduce)');

  function pathOf(url) {
    try {
      return new URL(url, window.location.href).pathname;
    } catch (_) {
      return null;
    }
  }
  function isOn(link, path) {
    return link.getAttribute('data-nav').split(' ').some((p) => (p === '/' ? path === '/' : path.startsWith(p)));
  }
  function markCurrent(path) {
    document.querySelectorAll('a[data-nav]').forEach((a) => {
      const on = isOn(a, path);
      a.classList.toggle('active', on);
      if (on) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
  }
  // Forward or back: along the bottom tabs when both screens are on them,
  // else along the menu, else further into a screen or back out of it.
  function direction(from, to) {
    const orders = [];
    const tabs = document.querySelector('.bottom-nav');
    if (tabs && tabs.getClientRects().length) orders.push([...tabs.querySelectorAll('a[data-nav]')]);
    orders.push([...document.querySelectorAll('.sidebar a[data-nav]')]);
    for (const links of orders) {
      const i = links.findIndex((a) => isOn(a, from));
      const j = links.findIndex((a) => isOn(a, to));
      if (i >= 0 && j >= 0 && i !== j) return j > i ? 'forward' : 'back';
    }
    return to.split('/').length >= from.split('/').length ? 'forward' : 'back';
  }

  function progress(on) {
    let el = document.getElementById('nav-progress');
    if (on) {
      if (el && el.classList.contains('done')) el.remove();
      else if (el) return;
      el = document.createElement('div');
      el.id = 'nav-progress';
      el.className = 'nav-progress';
      document.body.appendChild(el);
    } else if (el && !el.classList.contains('done')) {
      el.classList.add('done');
      setTimeout(() => el.remove(), 500);
    }
  }

  let going = null; // the screen a tap is on its way to, and which way
  let loading = 0;
  function settled(d) {
    loading = Math.max(0, loading - 1);
    // Failed or never answered. A 204 sending on to the next page (/loans to
    // the loan) is neither, though htmx never marks it successful.
    const status = d.xhr.status;
    if (d.failed || !status || status >= 400) {
      going = null;
      markCurrent(window.location.pathname);
    }
    if (!loading) progress(false);
  }
  document.addEventListener('htmx:beforeRequest', (e) => {
    const d = e.detail;
    if (d.target !== document.body) return;
    loading += 1;
    progress(true);
    // The request's own end: htmx's afterRequest goes to the element that
    // sent it, which a page swapped in first may have taken away.
    d.xhr.addEventListener('loadend', () => settled(d), { once: true });
    const verb = d.requestConfig && d.requestConfig.verb;
    if (d.boosted && verb === 'get') {
      const to = pathOf(d.requestConfig.path);
      const from = window.location.pathname;
      going = to && to !== from ? direction(from, to) : null;
      if (to) markCurrent(to);
    } else if (verb !== 'get') {
      // A save comes back where it was; it does not slide. A GET that is not
      // boosted follows a redirect (/loans to the loan), so keeps its way.
      going = null;
    }
  });
  document.addEventListener('htmx:beforeSwap', (e) => {
    const d = e.detail;
    if (d.target !== document.body || !d.shouldSwap) return;
    // Out of the old page before it is pictured to slide away.
    const bar = document.getElementById('nav-progress');
    if (bar) bar.remove();
    closeDrawer(true);
    if (going && !still.matches && document.startViewTransition) {
      document.documentElement.setAttribute('data-nav-dir', going);
      // As htmx swaps a page, to the top, but for a link to a place on the
      // page (/settings#tabs), which htmx scrolls to.
      const anchor = d.pathInfo && d.pathInfo.anchor;
      d.swapOverride = 'innerHTML transition:true' + (anchor ? '' : ' show:top');
    }
    going = null;
  });

  // -------------------------------------------------------------- the menu
  // A panel from the right. The button is a link to /more, the same menu as
  // a page, for when this script has not run.
  function drawer() {
    return document.getElementById('drawer');
  }
  function expanded(open) {
    document.documentElement.classList.toggle('drawer-open', open);
    document.querySelectorAll('[data-drawer-open]').forEach((b) => b.setAttribute('aria-expanded', String(open)));
  }
  function openDrawer() {
    const d = drawer();
    if (!d || d.open) return;
    d.classList.remove('closing', 'dragging');
    d.style.transform = '';
    d.showModal();
    expanded(true);
  }
  function closeDrawer(now) {
    const d = drawer();
    expanded(false);
    if (!d || !d.open) return;
    d.classList.remove('dragging');
    // From where a swipe left it, if one did.
    d.style.transform = '';
    if (now || still.matches) {
      d.classList.remove('closing');
      d.close();
      return;
    }
    d.classList.add('closing');
    // Its own slide ending, not a pressed button's or link's inside it.
    const shut = (e) => {
      if (e && (e.target !== d || e.propertyName !== 'transform')) return;
      d.removeEventListener('transitionend', shut);
      if (!d.classList.contains('closing')) return;
      d.classList.remove('closing');
      d.close();
    };
    d.addEventListener('transitionend', shut);
    setTimeout(shut, 350);
  }

  // A swipe to the right closes it. The click a swipe ends on goes nowhere.
  let swipe = null;
  let swiped = 0;
  document.addEventListener('pointerdown', (e) => {
    const d = e.target.closest && e.target.closest('dialog.drawer[open]');
    if (!d || e.pointerType === 'mouse') return;
    swipe = { d, x: e.clientX, y: e.clientY, at: e.timeStamp, dx: 0, on: false };
  });
  document.addEventListener('pointermove', (e) => {
    if (!swipe) return;
    const dx = e.clientX - swipe.x;
    const dy = e.clientY - swipe.y;
    if (!swipe.on) {
      if (Math.abs(dy) > 10 && Math.abs(dy) > Math.abs(dx)) swipe = null;
      if (!swipe || Math.abs(dx) < 10) return;
      swipe.on = true;
      swipe.d.classList.add('dragging');
    }
    swipe.dx = Math.max(0, dx);
    swipe.d.style.transform = `translateX(${swipe.dx}px)`;
  });
  function endSwipe(e) {
    if (!swipe) return;
    const { d, dx, on, at } = swipe;
    swipe = null;
    if (!on) return;
    swiped = e.timeStamp;
    if (dx > d.offsetWidth / 3 || dx / Math.max(1, e.timeStamp - at) > 0.5) {
      closeDrawer();
    } else {
      d.classList.remove('dragging');
      d.style.transform = '';
    }
  }
  document.addEventListener('pointerup', endSwipe);
  document.addEventListener('pointercancel', endSwipe);
  document.addEventListener('click', (e) => {
    if (swiped && e.timeStamp - swiped < 500 && e.target.closest('#drawer')) {
      e.preventDefault();
      e.stopPropagation();
    }
    swiped = 0;
  }, true);

  document.addEventListener('click', (e) => {
    if (e.target.closest('[data-drawer-open]')) {
      e.preventDefault();
      openDrawer();
      return;
    }
    const d = drawer();
    if (!d || !d.open) return;
    if (e.target === d) {
      // The backdrop is the dialog's, outside its box.
      if (e.clientX < d.getBoundingClientRect().left) closeDrawer();
    } else if (e.target.closest('[data-drawer-close], #drawer a[href]')) {
      closeDrawer();
    }
  });
  // The menu button is a link playing a button, so Space opens it too.
  document.addEventListener('keydown', (e) => {
    if (e.key !== ' ' || !e.target.closest || !e.target.closest('[data-drawer-open]')) return;
    e.preventDefault();
    openDrawer();
  });
  // Escape: slide it away rather than drop it.
  document.addEventListener('cancel', (e) => {
    if (e.target !== drawer()) return;
    e.preventDefault();
    closeDrawer();
  }, true);
  document.addEventListener('close', (e) => {
    if (e.target === drawer()) expanded(false);
  }, true);

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

  // ------------------------------------------------- keep the place on save
  // A save comes back to the page at the #anchor of what it changed, and htmx
  // scrolls that to the top. When it was already on screen, put it back where
  // it was instead, so the page does not jump under the person's finger.
  let place = null;
  document.addEventListener('htmx:beforeRequest', (e) => {
    const elt = e.detail.elt;
    if (!elt || elt.tagName !== 'FORM' || (elt.getAttribute('method') || '').toLowerCase() !== 'post') return;
    const tops = {};
    document.querySelectorAll('main [id]').forEach((el) => { tops[el.id] = el.getBoundingClientRect().top; });
    place = { tops };
  });
  document.addEventListener('htmx:afterSettle', () => {
    if (!place) return;
    const { tops } = place;
    place = null;
    // After htmx's own scroll to the anchor, which follows this event.
    setTimeout(() => {
      const id = decodeURIComponent(window.location.hash.slice(1));
      const el = id && document.getElementById(id);
      if (!el || !(id in tops)) return;
      const was = tops[id];
      if (was < 0 || was > window.innerHeight) return;
      window.scrollBy({ top: el.getBoundingClientRect().top - was, behavior: 'instant' });
    }, 0);
  });

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
  // The amount is per How often. Changing How often keeps the yearly cost, so
  // $2,097.50 every year is $349.58 every 2 months. Cycles divide 12, so the
  // yearly figure stays in whole cents; only the shown amount is rounded.
  function cents(text) {
    const m = String(text).replace(/[$,\s]/g, '').match(/^(\d*)(?:\.(\d{0,2}))?$/);
    if (!m || (m[1] === '' && !m[2])) return null;
    return Number(m[1] || '0') * 100 + Number(((m[2] || '') + '00').slice(0, 2));
  }
  function plain(c) {
    return Math.floor(c / 100) + '.' + String(c % 100).padStart(2, '0');
  }
  function roundDiv(n, d) {
    return Math.floor((2 * n + d) / (2 * d));
  }
  const aud = new Intl.NumberFormat('en-AU', { style: 'currency', currency: 'AUD' });
  function monthHint(row, yearly, cycle) {
    const hint = row.querySelector('[data-month-hint]');
    if (hint) hint.textContent = cycle > 1 && yearly !== null ? `${aud.format(roundDiv(yearly, 12) / 100)} a month` : '';
  }
  document.addEventListener('change', (e) => {
    const sel = e.target;
    if (!sel.matches || !sel.matches('select[data-cycle-select]')) return;
    const row = sel.closest('[data-budget-line]');
    const input = row.querySelector('input[data-amount-input]');
    const cycle = Number(sel.value);
    let yearly = row.dataset.yearly === '' ? null : Number(row.dataset.yearly);
    if (yearly === null) {
      const typed = cents(input.value);
      if (typed !== null) yearly = typed * (12 / Number(row.dataset.cycle));
    }
    if (yearly !== null) {
      input.value = plain(roundDiv(yearly * cycle, 12));
      row.dataset.yearly = String(yearly);
    }
    row.dataset.cycle = String(cycle);
    monthHint(row, yearly, cycle);
  });
  document.addEventListener('input', (e) => {
    const input = e.target;
    if (!input.matches || !input.matches('input[data-amount-input]')) return;
    const row = input.closest('[data-budget-line]');
    const cycle = Number(row.dataset.cycle);
    const typed = cents(input.value);
    const yearly = typed === null ? null : typed * (12 / cycle);
    row.dataset.yearly = yearly === null ? '' : String(yearly);
    monthHint(row, yearly, cycle);
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

  function init(root) {
    showKind(root);
    const flash = root.querySelector && root.querySelector('[data-flash]');
    if (flash && flash.textContent.trim()) toast(flash.textContent.trim());
  }

  document.addEventListener('DOMContentLoaded', () => {
    init(document);
    if (window.htmx) window.htmx.onLoad((el) => { if (el !== document.body) init(el); else init(document); });
  });
})();
