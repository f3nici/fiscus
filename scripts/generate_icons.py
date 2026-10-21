"""Render the PWA icons from scripts/icon.svg with Chromium (Playwright).

    python scripts/generate_icons.py

Writes fiscus/static/icons/*.png. Run by hand when the icon changes; the PNGs
are committed so the container build needs no browser.
"""

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SVG = (ROOT / 'scripts' / 'icon.svg').read_text()
OUT = ROOT / 'fiscus' / 'static' / 'icons'

# (file, size, padding as a fraction: maskable icons keep the art in the safe zone)
TARGETS = [
    ('icon-192.png', 192, 0.0),
    ('icon-512.png', 512, 0.0),
    ('maskable-512.png', 512, 0.12),
    ('apple-touch-icon.png', 180, 0.0),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    exe = os.environ.get('CHROMIUM_PATH')
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        page = browser.new_page()
        for name, size, pad in TARGETS:
            inset = int(size * pad)
            inner = size - 2 * inset
            html = (
                '<html><body style="margin:0;background:#f2f4f7">'
                f'<div style="width:{size}px;height:{size}px;display:flex;align-items:center;'
                f'justify-content:center">'
                f'<div style="width:{inner}px;height:{inner}px">{SVG}</div></div></body></html>'
            )
            page.set_viewport_size({'width': size, 'height': size})
            page.set_content(html)
            page.screenshot(
                path=str(OUT / name), clip={'x': 0, 'y': 0, 'width': size, 'height': size}
            )
            print(f'wrote {name}')
        browser.close()


if __name__ == '__main__':
    main()
