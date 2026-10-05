from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import any_user
from app.database import get_db
from app.models import User
from app.services import analytics_service

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/summary")
def summary(_: User = Depends(any_user), db: Session = Depends(get_db)):
    return analytics_service.summary(db)


@router.get("/response-times")
def response_times(limit: int = 200, _: User = Depends(any_user), db: Session = Depends(get_db)):
    return analytics_service.response_times(db, limit)


@router.get("/breakdowns")
def breakdowns(_: User = Depends(any_user), db: Session = Depends(get_db)):
    return analytics_service.breakdowns(db)


@router.get("/reroutes")
def reroutes(_: User = Depends(any_user), db: Session = Depends(get_db)):
    return analytics_service.reroutes(db)
