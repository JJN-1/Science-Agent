from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.store.models import LlmCache

DEFAULT_TTL_SECONDS = 604800  # 7 天


def get(session: Session, cache_key: str) -> LlmCache | None:
    """按键取缓存。命中已过期的行时顺手删掉它（按主键删除，再返回 None）。"""
    row = session.get(LlmCache, cache_key)
    if row is None:
        return None
    if _expired(row, datetime.now(UTC)):
        session.delete(row)
        session.flush()
        return None
    return row


def put(
    session: Session,
    *,
    cache_key: str,
    provider: str,
    model: str,
    tier: str,
    response: dict,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> LlmCache:
    """写入或覆盖一条缓存。过期清理交给 `purge_expired`（启动时调用），
    不在这里做批量删除——那会和同事务里刚 add 的行发生条件求值冲突。
    """
    now = datetime.now(UTC)
    row = session.get(LlmCache, cache_key)
    if row is None:
        row = LlmCache(
            cache_key=cache_key, provider=provider, model=model, tier=tier,
            response=response, created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        session.add(row)
    else:
        row.provider = provider
        row.model = model
        row.tier = tier
        row.response = response
        row.created_at = now
        row.expires_at = now + timedelta(seconds=ttl_seconds)
    session.flush()
    return row


def purge_expired(session: Session, now: datetime | None = None) -> int:
    """批量删除已过期行，返回删除条数（启动时调用）。

    用 ``synchronize_session=False``：这是一次纯粹的后台清理，不需要把结果同步回
    身份映射——反之 ORM 会拿会话里刚 add、尚未落库的行去求值删除条件，
    而 Python 侧是 aware datetime、列上是 naive datetime，比较会直接报错。
    """
    moment = now or datetime.now(UTC)
    naive = moment.replace(tzinfo=None) if moment.tzinfo else moment
    result = session.execute(
        sa_delete(LlmCache).where(LlmCache.expires_at <= naive),
        execution_options={"synchronize_session": False},
    )
    session.flush()
    return int(result.rowcount or 0)


def count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(LlmCache)) or 0)


def _expired(row: LlmCache, now: datetime) -> bool:
    expires_at = row.expires_at
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:  # SQLite 取出的是 naive UTC
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= now
