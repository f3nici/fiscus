"""Forecast invalidation.

Any flush that adds, changes or deletes a row a user owns bumps that user's
`data_version`. The forecast cache key includes it, so a stale forecast can
never be served after the data under it moved.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, update
from sqlalchemy.orm import Session

from fiscus.models import ForecastCache, Owned, User, UserSettings

_KEY = 'fiscus_touched_users'

# Settings that do not change a forecast are noise; low balance and horizon do.
_IGNORED = (ForecastCache,)


def _users_for(obj: Any) -> set[int]:
    if isinstance(obj, _IGNORED):
        return set()
    if isinstance(obj, Owned | UserSettings):
        uid = getattr(obj, 'user_id', None)
        return {uid} if uid else set()
    return set()


def install(session_cls: type[Session] = Session) -> None:
    if getattr(session_cls, '_fiscus_versioning', False):
        return
    session_cls._fiscus_versioning = True  # type: ignore[attr-defined]

    @event.listens_for(session_cls, 'before_flush')
    def _collect(db: Session, _ctx: Any, _instances: Any) -> None:
        touched: set[int] = db.info.setdefault(_KEY, set())
        with db.no_autoflush:
            for obj in list(db.new) + list(db.dirty) + list(db.deleted):
                if obj in db.dirty and not db.is_modified(obj, include_collections=False):
                    continue
                touched.update(_users_for(obj))

    @event.listens_for(session_cls, 'after_flush')
    def _bump(db: Session, _ctx: Any) -> None:
        touched: set[int] = db.info.pop(_KEY, set())
        if touched:
            db.connection().execute(
                update(User)
                .where(User.id.in_(touched))
                .values(data_version=User.data_version + 1)
                .execution_options(synchronize_session=False)
            )
