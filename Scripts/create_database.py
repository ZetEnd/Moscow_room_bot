import asyncio

import asyncpg

from app.config import settings


async def create_database():
    conn = await asyncpg.connect(
        user=settings.postgres_user,
        password=settings.postgres_password,
        host=settings.postgres_host,
        port=settings.postgres_port,
        database="postgres",  # connect to the default DB to issue CREATE DATABASE
    )
    exists = await conn.fetchval(
        "SELECT 1 FROM pg_database WHERE datname = $1", settings.postgres_db
    )
    if exists:
        print(f"Database '{settings.postgres_db}' already exists.")
    else:
        await conn.execute(f'CREATE DATABASE "{settings.postgres_db}"')
        print(f"Database '{settings.postgres_db}' created.")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(create_database())