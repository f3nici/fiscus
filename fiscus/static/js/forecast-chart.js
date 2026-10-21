/*
 * The forecast chart, drawn with the vendored uPlot. Combined balance, what
 * is free to spend after goals and the budget, an optional scenario, each with
 * its trend, each account, and the low-balance line, on the same axes. One
 * hue, on all accounts; everything else is ink or grey, and the account behind
 * a low-balance warning is drawn in the negative colour (D68). The dashboard
 * draws a compact one: free to spend alone.
 */
(function () {
  'use strict';

  // Greys for the accounts, darkest first, so a few still tell apart.
  const GREYS = ['#8a9099', '#b3b8bf', '#6b7079', '#c9cdd2', '#9aa0a8', '#7c828a', '#a6abb2'];

  function token(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function money(v) {
    if (v == null) return '';
    const neg = v < 0;
    const s = Math.abs(v / 100).toLocaleString('en-AU', { maximumFractionDigits: 0 });
    return (neg ? '-$' : '$') + s;
  }

  // Australian dates on the time axis: 16 Oct, and the year at January.
  function day(ts) {
    const d = new Date(ts * 1000);
    const dm = d.toLocaleDateString('en-AU', { day: 'numeric', month: 'short' });
    return d.getMonth() === 0 && d.getDate() <= 7 ? dm + ' ' + d.getFullYear() : dm;
  }

  // Round steps for the money axis: 1, 2, 2.5 or 5 of a power of ten, four
  // or five of them, with the top one above the highest value.
  function step(min, max) {
    const raw = Math.max(1, max - min) / 4;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  }
  function range(u, min, max) {
    const s = step(min, max);
    return [Math.floor(min / s) * s, Math.ceil(max / s) * s + (max % s === 0 ? s : 0)];
  }
  function splits(u, axisIdx, min, max) {
    const s = step(min, max);
    const out = [];
    for (let v = Math.ceil(min / s) * s; v <= max + 1e-6; v += s) out.push(v);
    return out;
  }

  function linePlugin(value, colour) {
    return {
      hooks: {
        draw: [
          (u) => {
            if (value == null) return;
            const [lo, hi] = [u.scales.y.min, u.scales.y.max];
            if (value < lo || value > hi) return;
            const ctx = u.ctx;
            ctx.save();
            const y = Math.round(u.valToPos(value, 'y', true)) + 0.5;
            ctx.strokeStyle = colour;
            ctx.lineWidth = window.devicePixelRatio || 1;
            ctx.setLineDash([4 * (window.devicePixelRatio || 1), 3 * (window.devicePixelRatio || 1)]);
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

  function axes(compact) {
    const muted = token('--muted', '#5d6269');
    const grid = token('--grid', '#e5e7eb');
    const font = '12px ' + (getComputedStyle(document.body).fontFamily || 'sans-serif');
    return [
      { stroke: muted, font, grid: { show: false }, ticks: { show: false }, space: compact ? 70 : 60, gap: 6, values: (u, vals) => vals.map(day) },
      { stroke: muted, font, size: compact ? 64 : 72, gap: 6, splits, values: (u, vals) => vals.map(money), grid: { stroke: grid, width: 1 }, ticks: { show: false } },
    ];
  }

  function draw(el) {
    if (el.dataset.drawn || !window.uPlot) return;
    // In an attribute, not a <script>: htmx drops script tags when it swaps a
    // boosted page (allowScriptTags is off), which left the chart blank.
    if (!el.dataset.chart) return;
    const data = JSON.parse(el.dataset.chart);
    el.dataset.drawn = '1';
    const compact = el.hasAttribute('data-compact');
    const hue = token('--chart', '#1d5fbf');
    const ink = token('--text', '#0a0a0a');
    const muted = token('--muted', '#5d6269');
    const negative = token('--negative', '#c4242a');
    const xs = data.dates.map((d) => Date.parse(d) / 1000);
    const lines = data.lines || {};
    const series = [{}];
    const values = [xs];
    const keys = [null];
    // Each line shows as saved, else as drawn by default.
    const add = (key, opts, vals, show) => {
      opts.show = Object.prototype.hasOwnProperty.call(lines, key) ? lines[key] : show;
      opts.value = (u, v) => money(v);
      opts.points = { show: false };
      series.push(opts);
      values.push(vals);
      keys.push(key);
    };
    if (compact) {
      add('free', { label: 'Free to spend', stroke: hue, width: 2 }, data.free, true);
    } else {
      add('combined', { label: 'All accounts', stroke: hue, width: 2 }, data.combined, true);
      if (data.trend) {
        add('trend', { label: 'Trend', stroke: hue, width: 1, dash: [5, 4] }, data.trend, true);
      }
      if (data.free) {
        add('free', { label: 'Free to spend', stroke: ink, width: 2 }, data.free, true);
        add('free_trend', { label: 'Free to spend trend', stroke: ink, width: 1, dash: [5, 4] }, data.free_trend, true);
      }
      if (data.scenario) {
        add('scenario', { label: data.scenario.name, stroke: muted, width: 2, dash: [8, 5] }, data.scenario.combined, true);
        add('scenario_trend', { label: data.scenario.name + ' trend', stroke: muted, width: 1, dash: [3, 3] }, data.scenario.trend, true);
      }
      data.accounts.forEach((a, i) => {
        add(a.key, { label: a.name, stroke: a.low ? negative : GREYS[i % GREYS.length], width: a.low ? 2 : 1.25 }, a.values, data.accounts.length <= 4 || a.low);
      });
    }
    const threshold = el.hasAttribute('data-threshold') ? Number(el.getAttribute('data-threshold')) : null;
    const height = compact ? 190 : Math.max(260, Math.min(400, Math.round(el.clientWidth * 0.42)));
    const opts = {
      width: el.clientWidth,
      height,
      series,
      scales: { x: { time: true }, y: { range } },
      axes: axes(compact),
      cursor: compact ? { drag: { x: false, y: false }, points: { size: 8 } } : { drag: { x: true, y: false }, points: { size: 8 } },
      legend: { show: !compact, live: true },
      plugins: [compact ? linePlugin(0, muted) : linePlugin(threshold, negative)],
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
