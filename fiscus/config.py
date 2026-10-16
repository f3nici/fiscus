"""Configuration from environment variables.

Everything the container needs is read here once, at start-up. Nothing else in
the package reads `os.environ`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

LOCAL_HOSTS = frozenset({'localhost', '127.0.0.1', '::1'})


class ConfigError(RuntimeError):
    pass


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == '':
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, '')
    if raw.strip() == '':
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f'{key} must be a whole number, got {raw!r}') from exc


@dataclass(frozen=True)
class Settings:
    base_url: str
    rp_id: str
    rp_name: str
    origin: str
    data_dir: Path
    timezone: str
    trusted_proxies: str
    log_level: str
    backup_retention_days: int
    backup_hour: int
    setup_link_hours: int
    bootstrap_hours: int
    session_idle_days: int
    session_absolute_days: int
    build_hash: str
    run_jobs: bool
    tax_dir: Path | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / 'fiscus.db'

    @property
    def database_url(self) -> str:
        return f'sqlite:///{self.db_path}'

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / 'uploads'

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / 'backups'

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def origin_is_https(self) -> bool:
        return urlsplit(self.origin).scheme == 'https'

    @property
    def origin_is_local(self) -> bool:
        return (urlsplit(self.origin).hostname or '') in LOCAL_HOSTS

    @property
    def passkeys_allowed(self) -> bool:
        """WebAuthn only runs over HTTPS, with localhost excepted for development."""
        return self.origin_is_https or self.origin_is_local

    @property
    def cookie_secure(self) -> bool:
        return self.origin_is_https

    @property
    def session_cookie(self) -> str:
        # The __Host- prefix pins the cookie to this exact origin, but browsers
        # only accept it with Secure, so plain-HTTP localhost gets a plain name.
        return '__Host-fiscus' if self.cookie_secure else 'fiscus_session'

    @property
    def csrf_cookie(self) -> str:
        return '__Host-fiscus-csrf' if self.cookie_secure else 'fiscus_csrf'


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    base_url = env.get('FISCUS_BASE_URL', 'http://localhost:8000').rstrip('/')
    origin = env.get('FISCUS_ORIGIN', '').rstrip('/') or base_url
    parts = urlsplit(origin)
    if parts.scheme not in {'http', 'https'} or not parts.hostname:
        raise ConfigError(f'FISCUS_ORIGIN must be an http(s) URL, got {origin!r}')
    rp_id = env.get('FISCUS_RP_ID', '') or parts.hostname
    host = parts.hostname
    if host != rp_id and not host.endswith('.' + rp_id):
        raise ConfigError(
            f'FISCUS_RP_ID {rp_id!r} must be the origin host {host!r} or a parent domain of it'
        )

    # FISCUS_SECRET_KEY was required once and never used; one still set is ignored.
    timezone = env.get('FISCUS_TIMEZONE', 'Australia/Perth')
    try:
        ZoneInfo(timezone)
    except Exception as exc:
        raise ConfigError(f'FISCUS_TIMEZONE {timezone!r} is not an IANA timezone') from exc

    backup_hour = _int(env, 'FISCUS_BACKUP_HOUR', 2)
    if not 0 <= backup_hour <= 23:
        raise ConfigError('FISCUS_BACKUP_HOUR must be 0 to 23')

    return Settings(
        base_url=base_url,
        rp_id=rp_id,
        rp_name=env.get('FISCUS_RP_NAME', 'Fiscus'),
        origin=origin,
        data_dir=Path(env.get('FISCUS_DATA_DIR', '/data')),
        timezone=timezone,
        trusted_proxies=env.get('FISCUS_TRUSTED_PROXIES', '127.0.0.1'),
        log_level=env.get('FISCUS_LOG_LEVEL', 'info').lower(),
        backup_retention_days=_int(env, 'FISCUS_BACKUP_RETENTION_DAYS', 14),
        backup_hour=backup_hour,
        setup_link_hours=_int(env, 'FISCUS_SETUP_LINK_HOURS', 48),
        bootstrap_hours=_int(env, 'FISCUS_BOOTSTRAP_HOURS', 24),
        session_idle_days=_int(env, 'FISCUS_SESSION_IDLE_DAYS', 30),
        session_absolute_days=_int(env, 'FISCUS_SESSION_ABSOLUTE_DAYS', 90),
        build_hash=env.get('FISCUS_BUILD_HASH', 'local'),
        run_jobs=_bool(env.get('FISCUS_RUN_JOBS'), True),
        tax_dir=Path(env['FISCUS_TAX_DIR']) if env.get('FISCUS_TAX_DIR') else None,
    )
