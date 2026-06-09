

from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Table
from sqlalchemy.orm import relationship
from database import Base
from datetime import datetime

search_hotel_association = Table(
    "search_hotel",
    Base.metadata,
    Column("search_id", Integer, ForeignKey("search_history.id")),
    Column("hotel_id",  Integer, ForeignKey("hotels.id")),
)

class Hotel(Base):
    __tablename__ = "hotels"

    id             = Column(Integer, primary_key=True, index=True)
    sno            = Column(Integer)
    name           = Column(String)
    address        = Column(String, nullable=True)
    phone          = Column(String, nullable=True)
    rating         = Column(String, nullable=True, default="N/A")   # ← NEW
    source         = Column(String, nullable=True)
    location_query = Column(String, index=True)

    searches = relationship("SearchHistory", secondary=search_hotel_association, back_populates="hotels")


class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    username        = Column(String, unique=True, index=True)
    email           = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    is_admin        = Column(Integer, default=0)
    created_at      = Column(DateTime, default=datetime.utcnow)  # ← ADDED

    search_history  = relationship("SearchHistory", back_populates="user")


class SearchHistory(Base):
    __tablename__ = "search_history"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"))
    location   = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    user   = relationship("User", back_populates="search_history")
    hotels = relationship("Hotel", secondary=search_hotel_association, back_populates="searches")