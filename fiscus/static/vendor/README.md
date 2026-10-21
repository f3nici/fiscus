# Vendored frontend libraries

Served from this origin and precached by the service worker. No CDN.

| File | Library | Version | Licence |
| --- | --- | --- | --- |
| htmx-2.0.11.min.js | htmx | 2.0.11 | 0BSD (htmx-LICENSE.txt) |
| uPlot-1.6.32.iife.min.js, uPlot-1.6.32.min.css | uPlot | 1.6.32 | MIT (uPlot-LICENSE.txt) |
| vuecal-4.10.2.css | vue-cal | 4.10.2 | MIT (vuecal-LICENSE.txt) |
| ../fonts/geist-1.8.1-variable.woff2 | Geist (npm `geist`, Geist-Variable.woff2) | 1.8.1 | OFL 1.1 (../fonts/Geist-LICENSE.txt) |

vue-cal's stylesheet is used on its own: the calendar markup is rendered on the
server with vue-cal's class names, so it lays out exactly as CareLane's roster
does. None of vue-cal's JavaScript is used.

To update one, download the tarball from registry.npmjs.org, copy the dist file
here under its new versioned name, update the reference in
`fiscus/templates/base.html`, and this table.
