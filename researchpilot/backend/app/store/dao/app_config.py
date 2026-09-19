from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.store.models import AppConfig


def get(session: Session, key: str) -> dict | None:
    row = session.get(AppConfig, key)
    return dict(row.value) if row is not None else None


def get_prefix(session: Session, prefix: str) -> dict[str, dict]:
    """按键前缀批量读取，返回 {去掉前缀后的 key: value}。"""
    rows = session.scalars(
        select(AppConfig).where(AppConfig.key.startswith(prefix))
    ).all()
    return {row.key[len(prefix):]: dict(row.value) for row in rows}


def put(session: Session, key: str, value: dict) -> AppConfig:
    """写入或覆盖（upsert）。value 必须是 JSON 可序列化的 dict。"""
    row = session.get(AppConfig, key)
    if row is None:
        row = AppConfig(key=key, value=value)
        session.add(row)
    else:
        row.value = value
    session.flush()
    return row


def delete(session: Session, key: str) -> bool:
    row = session.get(AppConfig, key)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True
