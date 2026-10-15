/*
 * The forecast chart, drawn with the vendored uPlot. Combined balance, what
 * is free to spend after goals and the budget, an optional scenario, each with
 * its trend, each account, and the low-balance line, on the same axes.
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

  function thresholdPlugin(threshold) {
    return {
      hooks: {
        draw: [
          (u) => {
            if (threshold == null) return;
            const ctx = u.ctx;
            ctx.save();
            const y = u.valToPos(threshold, 'y', true);
            ctx.strokeStyle = 'rgba(239,68,68,0.6)';
            ctx.setLineDash([6, 6]);
            ctx.beginPath();
            ctx.moveTo(u.bbox.left, y);
            ctx.lineTo(u.bbox.left + u.bbox.width, y);
            ctx.stroke();
            ctx.restore();
          },
        ],
      },
    };
  }

  // A line turned off or on in the legend is kept for the person, so it stays
  // that way after a reload and on their other devices (D50).
  function save(key, shown) {
    const meta = document.querySelector('meta[name="csrf-token"]');
    fetch('/forecast/lines', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'X-CSRF-Token': meta ? meta.content : '' },
      body: new URLSearchParams({ key: key, shown: shown ? 'true' : 'false' }),
    }).catch(() => {});
  }

  function draw(el) {
    if (el.dataset.drawn || !window.uPlot) return;
    // In an attribute, not a <script>: htmx drops script tags when it swaps a
    // boosted page (allowScriptTags is off), which left the chart blank.
    if (!el.dataset.chart) return;
    const data = JSON.parse(el.dataset.chart);
    el.dataset.drawn = '1';
    const xs = data.dates.map((d) => Date.parse(d) / 1000);
    const lines = data.lines || {};
    const series = [{}];
    const values = [xs];
    const keys = [null];
    // Each line shows as saved, else as drawn by default.
    const add = (key, opts, vals, show) => {
      opts.show = Object.prototype.hasOwnProperty.call(lines, key) ? lines[key] : show;
      opts.value = (u, v) => money(v);
      series.push(opts);
      values.push(vals);
      keys.push(key);
    };
    add('combined', { label: 'All accounts', stroke: '#ffffff', width: 2 }, data.combined, true);
    if (data.trend) {
      add('trend', { label: 'Trend', stroke: 'rgba(255,255,255,0.85)', width: 3 }, data.trend, true);
    }
    if (data.free) {
      add('free', { label: 'Free to spend', stroke: '#22c55e', width: 2 }, data.free, true);
      add('free_trend', { label: 'Free to spend trend', stroke: 'rgba(34,197,94,0.85)', width: 3 }, data.free_trend, true);
    }
    if (data.scenario) {
      add('scenario', { label: data.scenario.name, stroke: '#f59e0b', width: 2, dash: [8, 5] }, data.scenario.combined, true);
      add('scenario_trend', { label: data.scenario.name + ' trend', stroke: 'rgba(245,158,11,0.85)', width: 3 }, data.scenario.trend, true);
    }
    data.accounts.forEach((a, i) => {
      add(a.key, { label: a.name, stroke: COLOURS[i % COLOURS.length], width: a.low ? 2 : 1 }, a.values, data.accounts.length <= 4 || a.low);
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
      plugins: [thresholdPlugin(threshold)],
      hooks: {
        setSeries: [
          (u, i, o) => {
            if (!keys[i] || !o || o.show == null) return;
            lines[keys[i]] = o.show;
            save(keys[i], o.show);
          },
        ],
      },
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
