"""Run the server: `python -m fiscus`.

One uvicorn worker, deliberately: SQLite, the in-memory rate limiter and the
background jobs all assume a single process.
"""

from __future__ import annotations

import os

import uvicorn

from fiscus.config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        'fiscus.app:main_app',
        factory=True,
        host=os.environ.get('FISCUS_HOST', '0.0.0.0'),  # noqa: S104 (inside a container)
        port=int(os.environ.get('FISCUS_PORT', '8000')),
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxies,
        log_level=settings.log_level,
        server_header=False,
        access_log=settings.log_level == 'debug',
    )


if __name__ == '__main__':
    main()
