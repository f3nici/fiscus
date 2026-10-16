/*
 * Calendar behaviour, after vue-cal in CareLane's roster: arrows and the
 * title move through time, the menu switches view, a click on an event opens
 * its details, a click on an empty day starts a new bill on that date.
 *
 * Every screen opens on the month. Swipe left and right to page, and the
 * arrow keys do the same on a keyboard.
 */
(function () {
  'use strict';

  function go(url) {
    const target = document.getElementById('cal');
    if (window.htmx && target) {
      window.htmx.ajax('GET', url, { target: '#cal', swap: 'outerHTML', headers: { 'HX-Target': 'cal' } })
        .then(() => window.history.replaceState({}, '', url));
    } else {
      window.location.href = url;
    }
  }

  function bind(root) {
    const cal = root.id === 'cal' ? root : root.querySelector && root.querySelector('#cal');
    if (!cal || cal.dataset.bound) return;
    cal.dataset.bound = '1';

    cal.addEventListener('click', (e) => {
      const nav = e.target.closest('[data-cal-nav]');
      if (nav) {
        e.preventDefault();
        go(nav.getAttribute('href'));
        return;
      }
      if (e.target.closest('.vuecal__event')) return; // handled by data-sheet
      const cell = e.target.closest('[data-new-date]');
      if (cell && !e.target.closest('a, button')) {
        window.location.href = '/bills/new?date=' + cell.getAttribute('data-new-date');
      }
    });

    let x0 = null;
    let y0 = null;
    cal.addEventListener('touchstart', (e) => {
      x0 = e.touches[0].clientX;
      y0 = e.touches[0].clientY;
    }, { passive: true });
    cal.addEventListener('touchend', (e) => {
      if (x0 === null) return;
      const dx = e.changedTouches[0].clientX - x0;
      const dy = e.changedTouches[0].clientY - y0;
      x0 = null;
      if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) {
        const link = cal.querySelector(dx < 0 ? '.vuecal__arrow--next' : '.vuecal__arrow--prev');
        if (link) go(link.getAttribute('href'));
      }
    }, { passive: true });
  }

  document.addEventListener('keydown', (e) => {
    const cal = document.getElementById('cal');
    if (!cal || e.target.closest('input, select, textarea, dialog[open]')) return;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      const link = cal.querySelector(e.key === 'ArrowRight' ? '.vuecal__arrow--next' : '.vuecal__arrow--prev');
      if (link) go(link.getAttribute('href'));
    }
  });

  document.addEventListener('DOMContentLoaded', () => {
    bind(document);
    if (window.htmx) window.htmx.onLoad((el) => bind(el));
  });
})();
