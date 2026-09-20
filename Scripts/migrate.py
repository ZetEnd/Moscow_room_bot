"""One-time migration: offer_type columns, sent_listings table,
unique index on listings.cian_id. Idempotent — safe to run twice."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.database import engine

STATEMENTS = [
    # 1) new columns (IF NOT EXISTS = safe to re-run)
    "ALTER TABLE listings ADD COLUMN IF NOT EXISTS offer_type text",
    "ALTER TABLE user_filters ADD COLUMN IF NOT EXISTS offer_type text",

    # 2) remove duplicate cian_id rows if any, so the unique index can be created
    """DELETE FROM listings a USING listings b
       WHERE a.cian_id = b.cian_id AND a.id > b.id""",

    # 3) unique index required by ON CONFLICT (cian_id) upserts
    "CREATE UNIQUE INDEX IF NOT EXISTS listings_cian_id_uniq ON listings (cian_id)",

    # 4) sent-tracking table (no duplicates to users)
    """CREATE TABLE IF NOT EXISTS sent_listings (
           user_id    integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
           listing_id integer NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
           sent_at    timestamptz NOT NULL DEFAULT now(),
           PRIMARY KEY (user_id, listing_id)
       )""",
]


async def main() -> None:
    async with engine.begin() as conn:
        for stmt in STATEMENTS:
            await conn.execute(text(stmt))
            one_line = " ".join(stmt.split())
            print(f"OK: {one_line[:70]}")

        cols = await conn.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_name IN ('listings', 'user_filters', 'sent_listings') "
            "ORDER BY table_name, ordinal_position"
        ))
        print("\nCurrent structure:")
        for table, col in cols:
            print(f"  {table}.{col}")

    await engine.dispose()
    print("\nMigration complete.")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())