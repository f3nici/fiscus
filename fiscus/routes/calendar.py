"""The calendar: month, week, day and agenda views, after CareLane's roster."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from fiscus.models import User
from fiscus.services import calendar_events
from fiscus.web import ctx, current_user, get_db, render

router = APIRouter()


@router.get('/calendar')
def calendar_page(
    request: Request,
    view: str = '',
    date_: str = '',
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    raw = request.query_params.get('date', date_)
    try:
        focus = date.fromisoformat(raw) if raw else c.clock.today()
    except ValueError:
        focus = c.clock.today()
    # No view in the URL: the page script picks agenda on a narrow screen.
    chosen = view if view in calendar_events.VIEWS else 'month'
    grid = calendar_events.grid(db, c.clock, user, chosen, focus)
    upcoming = calendar_events.grid(db, c.clock, user, 'agenda', c.clock.today())
    template = (
        'calendar/_cal.html' if request.headers.get('hx-target') == 'cal' else 'calendar/index.html'
    )
    return render(
        request,
        template,
        {
            'grid': grid,
            'view_given': bool(view),
            'upcoming': upcoming.days[:14],
            'views': calendar_events.VIEWS,
        },
    )
