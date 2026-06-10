# schemas.py — HotelOut la `rating` field add பண்ணு
# (மத்த schemas same, just HotelOut update)

from pydantic import BaseModel
from typing import Optional


class HotelOut(BaseModel):
    id:             int
    sno:            int
    name:           str
    address:        Optional[str] = "N/A"
    phone:          Optional[str] = "N/A"
    rating:         Optional[str] = "N/A"    # ← ADD THIS
    source:         Optional[str] = None
    location_query: Optional[str] = None

    class Config:
        from_attributes = True   # pydantic v2  (orm_mode = True  for pydantic v1)


# ── மத்த schemas (உன்னோட file la இருக்குன்னு assume) ─────────────────────────

class UserCreate(BaseModel):
    username: str
    email:    str
    password: str

class UserOut(BaseModel):
    id:       int
    username: str
    email:    str
    is_admin: bool

    class Config:
        from_attributes = True

class Token(BaseModel):
    access_token: str
    token_type:   str
    is_admin:     bool
    username:     str

class SearchRequest(BaseModel):
    location:    str
    radius_km:   float = 10.0
    max_results: int   = 10
    refresh:     bool  = False

class AdminLogin(BaseModel):
    username: str
    password: str
