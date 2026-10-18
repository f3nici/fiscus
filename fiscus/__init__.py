"""Fiscus: self-hosted personal finance for an Australian household."""

from pathlib import Path

# VERSION at the root is the one place the version is set; the image copies it
# to /app/VERSION beside the package.
__version__ = (Path(__file__).resolve().parent.parent / 'VERSION').read_text().strip()
