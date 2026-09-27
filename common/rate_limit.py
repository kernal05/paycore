"""Rate limiting via Redis, token-bucket style, keyed per API key (falls
back to per-IP if no key). Redis is already a hard dependency of the
fraud engine, so this adds no new infrastructure.
"""
import time

import redis
from fastapi import HTTPException, Request

DEFAULT_LIMIT = 100          # requests
DEFAULT_WINDOW_SECONDS = 60  # per this many seconds


def check_rate_limit(
    redis_client: redis.Redis,
    key: str,
    limit: int = DEFAULT_LIMIT,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> None:
    """Fixed-window counter — simpler than a true token bucket and good
    enough here; the tradeoff (double-limit burst right at a window
    boundary) is called out explicitly rather than hidden.
    """
    redis_key = f"ratelimit:{key}:{int(time.time()) // window_seconds}"
    current = redis_client.incr(redis_key)
    if current == 1:
        redis_client.expire(redis_key, window_seconds)
    if current > limit:
        raise HTTPException(
            status_code=429,
            detail=f"rate limit exceeded: {limit} requests per {window_seconds}s",
            headers={"Retry-After": str(window_seconds)},
        )


def rate_limit_dependency(redis_client: redis.Redis, limit: int = DEFAULT_LIMIT):
    """Returns a FastAPI dependency closed over a specific redis client and
    limit, so each service can wire its own thresholds.
    """

    def _dep(request: Request):
        identity = request.headers.get("x-api-key") or (request.client.host if request.client else "unknown")
        check_rate_limit(redis_client, identity, limit=limit)

    return _dep
