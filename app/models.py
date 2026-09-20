from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    filters: Mapped[list["UserFilter"]] = relationship(back_populates="user")


class UserFilter(Base):
    """A saved search a user wants monitored — a user can have several."""
    __tablename__ = "user_filters"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    city: Mapped[str] = mapped_column(String(100), default="Москва")
    min_price: Mapped[int | None]
    max_price: Mapped[int | None]
    rooms: Mapped[int | None]          # None = any; 0 = studio; 1 = room/1-room flat
    offer_type: Mapped[str | None] = mapped_column(String(20))  # None = all
    is_active: Mapped[bool] = mapped_column(default=True)

    user: Mapped["User"] = relationship(back_populates="filters")


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    cian_id: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    url: Mapped[str] = mapped_column(Text)
    price: Mapped[int]
    rooms: Mapped[int | None]
    offer_type: Mapped[str | None] = mapped_column(String(20))  # 'flat' | 'room'
    address: Mapped[str | None] = mapped_column(Text)
    photo_url: Mapped[str | None] = mapped_column(Text)
    photos: Mapped[list | None] = mapped_column(JSONB)  # list of URLs, up to 6
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_active: Mapped[bool] = mapped_column(default=True)


class SentListing(Base):
    """Which listings were already shown to which user."""
    __tablename__ = "sent_listings"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                         primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"),
                                            primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Favorite(Base):
    """Listings a user liked (Tinder-style heart)."""
    __tablename__ = "favorites"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                         primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"),
                                            primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)