"""Background jobs, run inside the app's event loop.

Once a minute the loop checks what is due: the nightly backup at
FISCUS_BACKUP_HOUR local time, ntfy reminders at each user's chosen hour, and
housekeeping (expired challenges, sessions and abandoned uploads), and a
Square sync for anyone connected, at most every six hours.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete, select

from fiscus.clock import utcnow_naive
from fiscus.models import AuthChallenge, AuthSession, Import, SquareConnection, User
from fiscus.services import backup, reminders, square
from fiscus.web import AppContext

log = logging.getLogger('fiscus.jobs')


def housekeeping(c: AppContext) -> None:
    now = utcnow_naive(c.clock)
    with c.sessions() as db:
        db.execute(delete(AuthChallenge).where(AuthChallenge.expires_at <= now))
        db.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
        stale = (
            db.execute(
                select(Import).where(
                    Import.status == 'pending', Import.created_at < now - timedelta(days=1)
                )
            )
            .scalars()
            .all()
        )
        for imp in stale:
            if imp.stored_path:
                (c.settings.upload_dir / imp.stored_path).unlink(missing_ok=True)
            db.delete(imp)
        db.commit()
    # Budget and bill files staged for a preview nobody finished.
    cutoff = c.clock.now().timestamp() - 86400
    for pattern in ('budget-*', 'bills-*', 'housing-*'):
        for f in c.settings.upload_dir.glob(pattern):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)


def run_reminders(c: AppContext) -> int:
    total = 0
    with c.sessions() as db:
        for user in db.execute(select(User).where(User.is_active.is_(True))).scalars():
            try:
                total += reminders.send_for_user(db, c.clock, user)
                db.commit()
            except Exception:
                db.rollback()
                log.exception('reminders failed for user %s', user.id)
    return total


def run_square(c: AppContext) -> int:
    """Sync every Square connection that is due. Each user's failure is their own."""
    synced = 0
    now = utcnow_naive(c.clock)
    with c.sessions() as db:
        conns = [x for x in db.execute(select(SquareConnection)).scalars() if square.due(x, now)]
        for conn in conns:
            user = db.get(User, conn.user_id)
            if user is None or not user.is_active:
                continue
            try:
                square.sync(db, c.clock, user)
                synced += 1
            except square.SquareError as exc:
                log.warning('square sync for user %s: %s', user.id, exc)
            except Exception:
                db.rollback()
                log.exception('square sync failed for user %s', user.id)
                continue
            db.commit()
    return synced


def tick(c: AppContext, state: dict[str, str]) -> None:
    local = c.clock.local_now()
    hour_key = local.strftime('%Y-%m-%d %H')
    if state.get('hour') != hour_key:
        state['hour'] = hour_key
        housekeeping(c)
        run_reminders(c)
        run_square(c)
        if local.hour == c.settings.backup_hour and state.get('backup') != local.date().isoformat():
            path = backup.nightly(c.settings, c.engine, c.clock)
            state['backup'] = local.date().isoformat()
            log.info('nightly backup written to %s', path)


async def run_forever(c: AppContext) -> None:
    state: dict[str, str] = {}
    while True:
        try:
            await asyncio.to_thread(tick, c, state)
        except Exception:
            log.exception('background job failed')
        await asyncio.sleep(60)
