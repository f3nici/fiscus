"""SQLite backups through the online backup API, so a running app is safe to copy."""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine

from fiscus.clock import Clock
from fiscus.config import Settings

PREFIX = 'fiscus-'


@dataclass(frozen=True)
class BackupFile:
    name: str
    size: int
    modified: datetime


def _copy(settings: Settings, engine: Engine, dest: Path) -> None:
    raw = engine.raw_connection()
    try:
        src = raw.driver_connection
        assert isinstance(src, sqlite3.Connection)
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        raw.close()


def nightly(settings: Settings, engine: Engine, clock: Clock) -> Path:
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = clock.local_now().strftime('%Y%m%d-%H%M%S')
    dest = settings.backup_dir / f'{PREFIX}{stamp}.db'
    tmp = dest.with_suffix('.partial')
    _copy(settings, engine, tmp)
    tmp.replace(dest)
    prune(settings, clock)
    return dest


def prune(settings: Settings, clock: Clock) -> list[str]:
    cutoff = (
        clock.now().timestamp() - timedelta(days=settings.backup_retention_days).total_seconds()
    )
    removed = []
    for f in settings.backup_dir.glob(f'{PREFIX}*.db'):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed.append(f.name)
    return removed


def list_backups(settings: Settings) -> list[BackupFile]:
    files = sorted(settings.backup_dir.glob(f'{PREFIX}*.db'), reverse=True)
    return [
        BackupFile(f.name, f.stat().st_size, datetime.fromtimestamp(f.stat().st_mtime))  # noqa: DTZ006
        for f in files
    ]


def snapshot_to_temp(settings: Settings, engine: Engine, clock: Clock) -> Path:
    stamp = clock.local_now().strftime('%Y%m%d-%H%M%S')
    folder = Path(tempfile.mkdtemp(prefix='fiscus-download-', dir=settings.data_dir))
    dest = folder / f'{PREFIX}{stamp}.db'
    _copy(settings, engine, dest)
    return dest


def restore(settings: Settings, source: Path) -> Path:
    """Replace the live database with a backup. Run with the app stopped."""
    check = sqlite3.connect(source)
    try:
        result = check.execute('PRAGMA integrity_check').fetchone()
        if not result or result[0] != 'ok':
            raise RuntimeError(f'{source} failed its integrity check')
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'users' not in tables:
            raise RuntimeError(f'{source} is not a Fiscus database')
    finally:
        check.close()
    live = settings.db_path
    if live.exists():
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')  # noqa: DTZ005
        keep = settings.backup_dir / f'{PREFIX}pre-restore-{stamp}.db'
        settings.backup_dir.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(live)
        out = sqlite3.connect(keep)
        try:
            src.backup(out)
        finally:
            out.close()
            src.close()
    for suffix in ('-wal', '-shm'):
        Path(str(live) + suffix).unlink(missing_ok=True)
    src = sqlite3.connect(source)
    out = sqlite3.connect(live)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return live
