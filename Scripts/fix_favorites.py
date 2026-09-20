import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.config import settings
from app.database import engine

DDL = """
CREATE TABLE IF NOT EXISTS favorites (
    user_id    integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    listing_id integer NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, listing_id)
)
"""


async def main() -> None:
    print("Connecting to:")
    print(f"  host={settings.postgres_host} port={settings.postgres_port}")
    print(f"  db={settings.postgres_db}  user={settings.postgres_user}")

    async with engine.begin() as conn:
        res = await conn.execute(text("SELECT to_regclass('public.favorites')"))
        print(f"favorites exists BEFORE: {res.scalar()}")

        await conn.execute(text(DDL))

        res = await conn.execute(text("SELECT to_regclass('public.favorites')"))
        print(f"favorites exists AFTER:  {res.scalar()}")

    await engine.dispose()
    print("Done.")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())