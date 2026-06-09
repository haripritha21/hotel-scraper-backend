from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from jose import JWTError, jwt
from passlib.context import CryptContext
import logging

from database import SessionLocal, engine
import models, schemas
from models import Base
from scraper import fetch_places as run_scraper

Base.metadata.create_all(bind=engine)

logger = logging.getLogger(__name__)

app = FastAPI(title="Restaurant Finder API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "https://bucolic-monstera-7a9c43.netlify.app",
    ],
    ...
)
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SECRET_KEY = "hotel_finder_secret_key_2024"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def verify_password(plain, hashed):
    return pwd_context.verify(plain, hashed)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if not username:
            raise exc
    except JWTError:
        raise exc
    user = db.query(models.User).filter(models.User.username == username).first()
    if not user:
        raise exc
    return user

def get_current_admin(current_user: models.User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


# ── AUTH ROUTES ───────────────────────────────────────────────────────────────

@app.post("/signup", response_model=schemas.UserOut)
def signup(user: schemas.UserCreate, db: Session = Depends(get_db)):
    if db.query(models.User).filter(models.User.username == user.username).first():
        raise HTTPException(status_code=400, detail="Username already registered")
    if db.query(models.User).filter(models.User.email == user.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    new_user = models.User(
        username=user.username,
        email=user.email,
        hashed_password=get_password_hash(user.password),
        is_admin=False,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user


@app.post("/token", response_model=schemas.Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    token = create_access_token(
        {"sub": user.username}, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {"access_token": token, "token_type": "bearer", "is_admin": user.is_admin, "username": user.username}


@app.post("/admin/login", response_model=schemas.Token)
def admin_login(form_data: schemas.AdminLogin, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect credentials")
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Not an admin account")
    token = create_access_token(
        {"sub": user.username}, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {"access_token": token, "token_type": "bearer", "is_admin": True, "username": user.username}


@app.get("/me", response_model=schemas.UserOut)
def get_me(current_user: models.User = Depends(get_current_user)):
    return current_user


# ── SEARCH ROUTE ──────────────────────────────────────────────────────────────

@app.post("/search", response_model=list[schemas.HotelOut])
async def search_stays(
    req: schemas.SearchRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    search_record = models.SearchHistory(user_id=current_user.id, location=req.location)
    db.add(search_record)
    db.commit()
    db.refresh(search_record)

    cache_key = req.location.lower().strip()
    cached = db.query(models.Hotel).filter(models.Hotel.location_query == cache_key).all()
    if cached:
        return cached

    stays = await run_scraper(req.location, radius_km=req.radius_km, max_results=req.max_results)

    if not stays:
        raise HTTPException(
            status_code=503,
            detail="Could not find hotels for this location. Try a more specific area name.",
        )

    saved = []
    for idx, stay in enumerate(stays):
        # ── FIX: Save rating as its own column ───────────────────────────────
        rating_val = stay.get("rating", "N/A")
        if not rating_val or rating_val == "N/A":
            rating_val = "N/A"

        record = models.Hotel(
            sno=idx + 1,
            name=stay.get("name", "N/A"),
            address=stay.get("address", "N/A"),
            phone=stay.get("phone", "N/A"),
            rating=rating_val,          # ← saved to its own DB column now
            source="Google Maps",
            location_query=cache_key,
        )
        db.add(record)
        saved.append(record)

    db.commit()
    for s in saved:
        db.refresh(s)

    search_record.hotels = saved
    db.commit()

    return saved


# ── ADMIN ROUTES ──────────────────────────────────────────────────────────────

@app.get("/admin/users", response_model=list[schemas.UserOut])
def list_users(db: Session = Depends(get_db), admin: models.User = Depends(get_current_admin)):
    return db.query(models.User).all()


@app.get("/admin/searches")
def list_searches(db: Session = Depends(get_db), admin: models.User = Depends(get_current_admin)):
    searches = (
        db.query(models.SearchHistory)
        .order_by(models.SearchHistory.created_at.desc())
        .all()
    )
    return [
        {
            "id": s.id,
            "user_id": s.user_id,
            "location": s.location,
            "created_at": s.created_at,
            "hotel_count": len(s.hotels),
        }
        for s in searches
    ]


@app.get("/admin/hotels", response_model=list[schemas.HotelOut])
def list_all_stays(db: Session = Depends(get_db), admin: models.User = Depends(get_current_admin)):
    return db.query(models.Hotel).all()


@app.delete("/admin/hotels/{hotel_id}")
def remove_stay(
    hotel_id: int,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    record = db.query(models.Hotel).filter(models.Hotel.id == hotel_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    db.delete(record)
    db.commit()
    return {"message": "Record deleted"}


@app.delete("/admin/users/{user_id}")
def remove_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    db.delete(user)
    db.commit()
    return {"message": "User deleted"}


@app.post("/admin/create-admin", response_model=schemas.UserOut)
def create_admin_user(
    user: schemas.UserCreate,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    if db.query(models.User).filter(models.User.username == user.username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    new_admin = models.User(
        username=user.username,
        email=user.email,
        hashed_password=get_password_hash(user.password),
        is_admin=True,
    )
    db.add(new_admin)
    db.commit()
    db.refresh(new_admin)
    return new_admin


# ── SEED ADMIN ────────────────────────────────────────────────────────────────

@app.on_event("startup")
def seed_admin():
    db = SessionLocal()
    try:
        existing = db.query(models.User).filter(models.User.username == "admin").first()
        if not existing:
            admin = models.User(
                username="admin",
                email="admin@hotelfinder.com",
                hashed_password=get_password_hash("admin123"),
                is_admin=True,
            )
            db.add(admin)
            db.commit()
            print("✅ Default admin created: admin / admin123")
        print("✅ Playwright scraper ready — no API key needed")
    finally:
        db.close()
        
