from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import admin, any_user
from app.database import get_db
from app.models import User
from app.schemas.schemas import LoginRequest, TokenResponse, UserCreate
from app.services.auth_service import create_token, hash_password, verify_password

router = APIRouter(prefix="/api", tags=["auth"])


@router.post("/auth/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username))
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid username or password")
    return TokenResponse(access_token=create_token(user.username, user.role), username=user.username, role=user.role)


@router.get("/auth/me")
def me(user: User = Depends(any_user)):
    return {"username": user.username, "role": user.role, "full_name": user.full_name}


@router.get("/users")
def list_users(_: User = Depends(admin), db: Session = Depends(get_db)):
    return [{"id": str(u.id), "username": u.username, "role": u.role, "full_name": u.full_name,
             "is_active": u.is_active, "created_at": u.created_at} for u in db.scalars(select(User).order_by(User.username))]


@router.post("/users", status_code=201)
def create_user(body: UserCreate, _: User = Depends(admin), db: Session = Depends(get_db)):
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, "username already exists")
    u = User(username=body.username, full_name=body.full_name, role=body.role, password_hash=hash_password(body.password))
    db.add(u)
    db.commit()
    return {"id": str(u.id), "username": u.username, "role": u.role}
