"""Command line for the operator. Run inside the container.

    python -m fiscus.cli recovery-link <username>   break-glass: a recovery link
    python -m fiscus.cli sign-out <username>        end every session (a lost device)
    python -m fiscus.cli bootstrap                  print a fresh bootstrap link
    python -m fiscus.cli backup                     write a backup now
    python -m fiscus.cli restore <file>             replace the database (app stopped)
    python -m fiscus.cli users                      list accounts
    python -m fiscus.cli remove-user <username>     delete a person and all their data

Every command that changes something writes to the audit log.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from fiscus.clock import Clock
from fiscus.config import load_settings
from fiscus.db import make_engine, make_sessionmaker, run_migrations
from fiscus.models import User
from fiscus.services import audit, auth, backup, versioning


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='fiscus.cli', description='Fiscus operator commands')
    sub = parser.add_subparsers(dest='cmd', required=True)
    rec = sub.add_parser('recovery-link', help='issue a one-time recovery link for a user')
    rec.add_argument('username')
    out = sub.add_parser('sign-out', help='end every session of a user, for a lost device')
    out.add_argument('username')
    sub.add_parser('bootstrap', help='print a bootstrap link (only while there are no users)')
    sub.add_parser('backup', help='write a backup to /data/backups now')
    res = sub.add_parser('restore', help='replace the database with a backup (stop the app first)')
    res.add_argument('file', type=Path)
    res.add_argument('--yes', action='store_true', help='do not ask for confirmation')
    sub.add_parser('users', help='list users')
    rem = sub.add_parser('remove-user', help='delete a person and everything they own')
    rem.add_argument('username')
    rem.add_argument('--yes', action='store_true', help='do not ask for confirmation')
    args = parser.parse_args(argv)

    settings = load_settings()
    clock = Clock(settings.tz)

    if args.cmd == 'restore':
        if not args.file.exists():
            print(f'{args.file} does not exist', file=sys.stderr)
            return 1
        if not args.yes:
            answer = input(f'Replace {settings.db_path} with {args.file}? Type "restore": ')
            if answer.strip() != 'restore':
                print('Cancelled.')
                return 1
        live = backup.restore(settings, args.file)
        run_migrations(settings.database_url)
        print(f'Restored {args.file} to {live}.')
        print(f'The previous database was kept in {settings.backup_dir}.')
        return 0

    run_migrations(settings.database_url)
    versioning.install()
    engine = make_engine(settings.database_url)
    sessions = make_sessionmaker(engine)

    if args.cmd == 'backup':
        path = backup.nightly(settings, engine, clock)
        print(f'Backup written to {path}')
        return 0

    with sessions() as db:
        if args.cmd == 'users':
            rows = db.execute(select(User).order_by(User.username)).scalars().all()
            if not rows:
                print('No users yet.')
            for u in rows:
                flags = ', '.join(
                    f
                    for f, on in (
                        ('admin', u.is_admin),
                        ('disabled', not u.is_active),
                    )
                    if on
                )
                print(
                    f'{u.username:<20} {u.display_name:<30} {len(u.credentials)} passkeys  {flags}'
                )
            return 0
        if args.cmd == 'bootstrap':
            link = auth.ensure_bootstrap(db, settings, clock)
            db.commit()
            if link is None:
                print('There are already users. Use recovery-link <username> instead.')
                return 1
            print(link.url)
            return 0
        if args.cmd == 'remove-user':
            return _remove_user(db, clock, args.username, confirmed=args.yes)
        if args.cmd in ('recovery-link', 'sign-out'):
            user = db.execute(
                select(User).where(User.username == args.username.strip().lower())
            ).scalar_one_or_none()
            if user is None:
                print(f'No user called {args.username}', file=sys.stderr)
                return 1
        if args.cmd == 'sign-out':
            assert user is not None
            ended = auth.end_sessions(db, user)
            audit.record(db, clock, 'cli.sign_out', subject_id=user.id, count=ended)
            db.commit()
            print(f'Signed {user.username} out of {ended} session{"" if ended == 1 else "s"}.')
            return 0
        if args.cmd == 'recovery-link':
            assert user is not None
            if not user.is_active:
                user.is_active = True
                audit.record(db, clock, 'user.enabled', subject_id=user.id, via='cli')
            link = auth.issue_link(db, settings, clock, 'recovery', user, None)
            audit.record(db, clock, 'cli.recovery_link', subject_id=user.id)
            db.commit()
            print(
                f'Recovery link for {user.username} (expires {link.expires_at:%d %b %Y %H:%M} UTC):'
            )
            print(link.url)
            return 0
    return 2


def _remove_user(db: Session, clock: Clock, username: str, *, confirmed: bool) -> int:
    """Delete a person and, through the database's cascades, every row they own.
    Fiscus is kept by one person now (D52); this clears out anyone else."""
    name = username.strip().lower()
    users = db.execute(select(User)).scalars().all()
    user = next((u for u in users if u.username == name), None)
    if user is None:
        print(f'No user called {username}', file=sys.stderr)
        return 1
    if len(users) == 1:
        print(f'{user.username} is the only person here. Nothing removed.', file=sys.stderr)
        return 1
    if user.is_admin and not any(u.is_admin and u.is_active for u in users if u is not user):
        print(
            f'{user.username} is the only admin, who keeps the backups. Nothing removed.',
            file=sys.stderr,
        )
        return 1
    if not confirmed:
        answer = input(
            f'Delete {user.username} and all their accounts, bills, pay and budgets? '
            'Type their username: '
        )
        if answer.strip().lower() != user.username:
            print('Cancelled.')
            return 1
    uid, uname = user.id, user.username
    db.expunge(user)
    # A plain DELETE, so SQLite's ON DELETE CASCADE takes every owned row.
    db.execute(delete(User).where(User.id == uid))
    audit.record(db, clock, 'user.removed', subject_id=uid, via='cli', username=uname)
    db.commit()
    print(f'Removed {uname} and everything they owned.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
