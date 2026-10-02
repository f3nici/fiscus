/*
 * The forecast chart, drawn with the vendored uPlot. Combined balance, each
 * account, an optional scenario on the same axes, the low-balance line, and
 * markers for paydays and large bills.
 */
(function () {
  'use strict';

  const COLOURS = ['#2563eb', '#14b8a6', '#f59e0b', '#a855f7', '#ec4899', '#0ea5e9', '#84cc16'];

  function money(v) {
    if (v == null) return '';
    const neg = v < 0;
    const s = Math.abs(v / 100).toLocaleString('en-AU', { maximumFractionDigits: 0 });
    return (neg ? '-$' : '$') + s;
  }

  function markersPlugin(markers, xs) {
    return {
      hooks: {
        draw: [
          (u) => {
            const ctx = u.ctx;
            ctx.save();
            markers.forEach((m) => {
              const x = u.valToPos(Date.parse(m.date) / 1000, 'x', true);
              if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) return;
              ctx.fillStyle = m.kind === 'pay' ? '#22c55e' : '#ef4444';
              ctx.beginPath();
              ctx.arc(x, u.bbox.top + u.bbox.height - 6 * devicePixelRatio, 3.5 * devicePixelRatio, 0, 2 * Math.PI);
              ctx.fill();
            });
            if (xs.threshold != null) {
              const y = u.valToPos(xs.threshold, 'y', true);
              ctx.strokeStyle = 'rgba(239,68,68,0.6)';
              ctx.setLineDash([6, 6]);
              ctx.beginPath();
              ctx.moveTo(u.bbox.left, y);
              ctx.lineTo(u.bbox.left + u.bbox.width, y);
              ctx.stroke();
            }
            ctx.restore();
          },
        ],
      },
    };
  }

  function draw(el) {
    if (el.dataset.drawn || !window.uPlot) return;
    const src = document.getElementById(el.getAttribute('data-source'));
    if (!src) return;
    const data = JSON.parse(src.textContent);
    el.dataset.drawn = '1';
    const xs = data.dates.map((d) => Date.parse(d) / 1000);
    const series = [{}];
    const values = [xs];
    series.push({ label: 'All accounts', stroke: '#ffffff', width: 2, value: (u, v) => money(v) });
    values.push(data.combined);
    if (data.scenario) {
      series.push({ label: data.scenario.name, stroke: '#f59e0b', width: 2, dash: [8, 5], value: (u, v) => money(v) });
      values.push(data.scenario.combined);
    }
    data.accounts.forEach((a, i) => {
      series.push({ label: a.name, stroke: COLOURS[i % COLOURS.length], width: 1, show: data.accounts.length <= 4, value: (u, v) => money(v) });
      values.push(a.values);
    });
    const threshold = el.hasAttribute('data-threshold') ? Number(el.getAttribute('data-threshold')) : null;
    const opts = {
      width: el.clientWidth,
      height: Math.max(280, Math.min(420, Math.round(el.clientWidth * 0.55))),
      series,
      scales: { x: { time: true } },
      axes: [
        { stroke: '#999', grid: { stroke: 'rgba(255,255,255,0.06)' }, ticks: { stroke: 'rgba(255,255,255,0.1)' } },
        { stroke: '#999', size: 70, values: (u, vals) => vals.map(money), grid: { stroke: 'rgba(255,255,255,0.06)' }, ticks: { stroke: 'rgba(255,255,255,0.1)' } },
      ],
      cursor: { drag: { x: true, y: false } },
      legend: { live: true },
      plugins: [markersPlugin(data.markers || [], { threshold })],
    };
    const plot = new window.uPlot(opts, values, el);
    const resize = () => plot.setSize({ width: el.clientWidth, height: opts.height });
    window.addEventListener('resize', resize);
  }

  function bind(root) {
    (root.querySelectorAll ? root : document).querySelectorAll('[data-forecast-chart]').forEach(draw);
  }

  document.addEventListener('DOMContentLoaded', () => {
    bind(document);
    if (window.htmx) window.htmx.onLoad((el) => bind(el));
  });
})();
