"""Read-only production checks. Never starts a Telegram poller or migrates data."""

import asyncio
import shutil

import aiohttp
import edgedb
from redis.asyncio import Redis

from msu_hub_bot.settings import settings


async def check():
    settings.validate_core()
    redis = Redis(host=settings.redis_host, port=settings.redis_port, password=settings.redis_password or None, db=settings.redis_db)
    database = edgedb.create_async_client(
        dsn=settings.edgedb_dsn, tls_ca=settings.edgedb_tls_ca or None, tls_security=settings.edgedb_tls_security
    )
    try:
        await asyncio.wait_for(redis.ping(), 15)
        assert await asyncio.wait_for(database.query_single("SELECT 1"), 15) == 1
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(f"https://api.telegram.org/bot{settings.bot_token}/getMe", proxy=settings.proxy or None) as response:
                if response.status != 200 or not (await response.json()).get("ok"):
                    raise RuntimeError("Telegram credential check failed")
        for program in ("ffmpeg", "ffprobe", "tesseract"):
            if not shutil.which(program):
                raise RuntimeError("Required media program is absent")
    finally:
        await redis.aclose()
        await database.aclose()


if __name__ == "__main__":
    from msu_hub_bot.redaction import install_redaction

    install_redaction()
    try:
        asyncio.run(check())
    except Exception:
        raise SystemExit("Production preflight failed; configuration and dependency checks did not pass") from None
    print("Production preflight passed")
