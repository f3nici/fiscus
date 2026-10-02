"""Tax estimate. Every figure on the page is labelled an estimate."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from fiscus.domain.tax import TaxConfigError
from fiscus.models import User
from fiscus.services import taxes
from fiscus.web import ctx, current_user, get_db, render

router = APIRouter()


@router.get('/tax')
def tax_page(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    c = ctx(request)
    try:
        view = taxes.view(db, user, c.clock.today(), c.tax_dir)
    except TaxConfigError as exc:
        return render(
            request, 'error.html', {'title': 'No tax table', 'message': str(exc)}, status_code=500
        )
    return render(request, 'tax/index.html', {'v': view})
