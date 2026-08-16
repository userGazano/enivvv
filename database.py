import logging
from datetime import datetime
from typing import Optional

import asyncpg

from config import DATABASE_URL

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def init_db() -> None:
    global _pool

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    _pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=60,
    )

    async with _pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id BIGSERIAL PRIMARY KEY,
                phone TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                session_path TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_cleanup_at TIMESTAMPTZ,
                last_cleanup_result TEXT
            );

            CREATE TABLE IF NOT EXISTS maintenance_logs (
                id BIGSERIAL PRIMARY KEY,
                account_id BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                operation TEXT NOT NULL,
                status TEXT NOT NULL,
                details TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

    logger.info("PostgreSQL initialized")


async def close_db() -> None:
    global _pool

    if _pool is not None:
        await _pool.close()
        _pool = None


def _require_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database is not initialized")
    return _pool


async def upsert_account(
    phone: str,
    name: str,
    session_path: str,
) -> int:
    pool = _require_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO accounts (phone, name, session_path)
            VALUES ($1, $2, $3)
            ON CONFLICT (phone)
            DO UPDATE SET
                name = EXCLUDED.name,
                session_path = EXCLUDED.session_path,
                updated_at = NOW()
            RETURNING id
            """,
            phone,
            name,
            session_path,
        )

    return int(row["id"])


async def get_account(account_id: int):
    pool = _require_pool()

    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT *
            FROM accounts
            WHERE id = $1
            """,
            account_id,
        )


async def list_accounts():
    pool = _require_pool()

    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT *
            FROM accounts
            ORDER BY id ASC
            """
        )


async def save_maintenance_result(
    account_id: int,
    operation: str,
    status: str,
    details: str,
) -> None:
    pool = _require_pool()

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO maintenance_logs (
                account_id,
                operation,
                status,
                details
            )
            VALUES ($1, $2, $3, $4);

            UPDATE accounts
            SET
                last_cleanup_at = NOW(),
                last_cleanup_result = $4,
                updated_at = NOW()
            WHERE id = $1;
            """,
            account_id,
            operation,
            status,
            details,
        )
