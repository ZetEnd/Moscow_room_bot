from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.models import Favorite, Listing, User, UserFilter

DATABASE_URL = (
    f"postgresql+asyncpg://"
    f"{settings.postgres_user}:"
    f"{settings.postgres_password}@"
    f"{settings.postgres_host}:"
    f"{settings.postgres_port}/"
    f"{settings.postgres_db}"
)

engine = create_async_engine(DATABASE_URL, echo=False)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ------------------------------------------------------------- users

async def get_or_create_user(session: AsyncSession, telegram_id: int,
                             username: str | None = None) -> User:
    res = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = res.scalar_one_or_none()

    if user is None:
        user = User(telegram_id=telegram_id, username=username)
        session.add(user)
        await session.flush()
        session.add(UserFilter(user_id=user.id))
        await session.commit()
        return user

    changed = False
    if not user.is_active:
        user.is_active = True
        changed = True

    res = await session.execute(
        select(UserFilter).where(UserFilter.user_id == user.id)
    )
    if res.scalars().first() is None:
        session.add(UserFilter(user_id=user.id))
        changed = True

    if changed:
        await session.commit()
    return user


# ------------------------------------------------------------- filter

async def set_min_price(session: AsyncSession, user_id: int,
                        min_price: int | None) -> None:
    """Only min_price matters now; everything else stays open."""
    res = await session.execute(select(UserFilter).where(UserFilter.user_id == user_id))
    f = res.scalars().first()
    if f is None:
        f = UserFilter(user_id=user_id)
        session.add(f)
    f.min_price = min_price
    f.max_price = None
    f.offer_type = None
    f.rooms = None
    f.is_active = True
    await session.commit()


async def get_user_filter(session: AsyncSession, user_id: int) -> UserFilter | None:
    res = await session.execute(
        select(UserFilter).where(UserFilter.user_id == user_id)
    )
    return res.scalars().first()


def _match_conditions():
    return (
        UserFilter.is_active.is_(True),
        Listing.is_active.is_(True),
        Listing.price > 0,
        or_(UserFilter.min_price.is_(None), Listing.price >= UserFilter.min_price),
    )


# ------------------------------------------------------------- listings

async def upsert_listings(session: AsyncSession, offers: list[dict]) -> list[Listing]:
    """Insert new listings / refresh known ones. Returns only NEW listings."""
    if not offers:
        return []

    ids = [o["cian_id"] for o in offers]
    existing = set((await session.execute(
        select(Listing.cian_id).where(Listing.cian_id.in_(ids))
    )).scalars())
    new_ids = [i for i in ids if i not in existing]

    values = [{
        "cian_id": o["cian_id"],
        "url": o["url"],
        "price": o["price"] or 0,
        "rooms": o["rooms"],
        "offer_type": o["offer_type"],
        "address": o["address"],
        "photo_url": o["photo_url"],
        "photos": o.get("photos"),
        "is_active": True,
    } for o in offers]

    stmt = pg_insert(Listing).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Listing.__table__.c.cian_id],
        set_={"price": stmt.excluded.price, "photos": stmt.excluded.photos,
              "is_active": True},
    )
    await session.execute(stmt)
    await session.commit()

    if not new_ids:
        return []
    res = await session.execute(select(Listing).where(Listing.cian_id.in_(new_ids)))
    return list(res.scalars().all())


async def wipe_session_data(session: AsyncSession) -> int:
    """Erase all listings EXCEPT favorited ones. Favorites survive the wipe."""
    res = await session.execute(
        delete(Listing).where(~Listing.id.in_(select(Favorite.listing_id)))
    )
    await session.commit()
    return res.rowcount


async def get_next_listing(session: AsyncSession, user_id: int,
                           exclude_ids: list[int] | None = None) -> Listing | None:
    """Cheapest matching listing, excluding only ids from the CURRENT session."""
    conds = [*_match_conditions()]
    if exclude_ids:
        conds.append(~Listing.id.in_(exclude_ids))
    stmt = (
        select(Listing)
        .distinct()
        .join(UserFilter, UserFilter.user_id == user_id)
        .where(*conds)
        .order_by(Listing.price.asc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


# ------------------------------------------------------------- favorites

async def toggle_favorite(session: AsyncSession, user_id: int, listing_id: int) -> bool:
    res = await session.execute(
        select(Favorite).where(Favorite.user_id == user_id,
                               Favorite.listing_id == listing_id)
    )
    fav = res.scalar_one_or_none()
    if fav:
        await session.delete(fav)
        await session.commit()
        return False
    session.add(Favorite(user_id=user_id, listing_id=listing_id))
    await session.commit()
    return True


async def get_favorite_ids(session: AsyncSession, user_id: int,
                           listing_ids: list[int]) -> set[int]:
    if not listing_ids:
        return set()
    res = await session.execute(
        select(Favorite.listing_id).where(Favorite.user_id == user_id,
                                          Favorite.listing_id.in_(listing_ids))
    )
    return set(res.scalars().all())


async def get_favorites(session: AsyncSession, user_id: int,
                        limit: int = 20) -> list[Listing]:
    stmt = (
        select(Listing)
        .join(Favorite, Favorite.listing_id == Listing.id)
        .where(Favorite.user_id == user_id, Listing.is_active.is_(True))
        .order_by(Favorite.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())