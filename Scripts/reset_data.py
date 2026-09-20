# Scripts\reset_data.py
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE sent_listings"))
        await conn.execute(text("TRUNCATE listings CASCADE"))
    await engine.dispose()
    print("listings and sent_listings cleared.")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())