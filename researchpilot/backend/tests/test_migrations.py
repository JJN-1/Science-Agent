"""migration 安全性：升级只加表，不动已有数据。

Sprint 3 一口气加了 5 张表（`budget_grants` / `app_config` / `llm_cache` / `jobs` /
`job_events`）。加表迁移最常见的翻车方式是顺手把已有表重建一遍 —— 数据没了而不自知。
这里用「升级前后逐表行数不变 + 新表确实出现」把它钉住。

另外 `upgrade head` 在每次启动时都会被无条件调用一次（零配置首启），所以
「重复执行是 no-op」也是必须成立的契约。
"""

from __future__ import annotations

from alembic import command
from sqlalchemy import inspect, text

from app.store.dao import projects as projects_dao
from app.store.db import make_engine, make_session_factory
from app.store.migrations import alembic_config, upgrade_to_head
from app.store.models import Project

# 加 app_config / llm_cache / jobs / job_events 之前的那一版（Sprint 3 budget grants）
BEFORE_JOBS = "9f3c1a7b5d20"
ADDED_TABLES = ("app_config", "llm_cache", "jobs", "job_events")


def _table_names(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _row_counts(engine, tables) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            table: int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
            for table in sorted(tables)
        }


def _seed_project(engine, title: str) -> int:
    """在「当前版本」的库上落一份真实数据，返回 project id。

    `projects` 表自 initial migration 起没被改过（没有 add_column / drop_column），
    所以比它新的迁移都能用同一套 ORM 写进去。
    """
    session = make_session_factory(engine)()
    try:
        project = projects_dao.create(session, title=title, domain="cs-ai", goal="G")
        session.commit()
        return project.id
    finally:
        session.close()


def test_upgrade_adds_new_tables_without_touching_existing_rows(tmp_path):
    engine = make_engine(tmp_path / "app.db")
    try:
        command.upgrade(alembic_config(engine), BEFORE_JOBS)
        assert not (set(ADDED_TABLES) & _table_names(engine)), "旧版本上就已经有新表了"

        project_id = _seed_project(engine, "迁移前")
        before = _row_counts(engine, _table_names(engine))

        upgrade_to_head(engine)

        assert set(ADDED_TABLES) <= _table_names(engine)
        # 老表行数一个都不能变（新表不在 before 里，单独比）
        assert _row_counts(engine, before.keys()) == before

        session = make_session_factory(engine)()
        try:
            assert session.get(Project, project_id).title == "迁移前"
        finally:
            session.close()
    finally:
        engine.dispose()


def test_upgrade_to_head_is_idempotent(tmp_path):
    """启动路径会重复调用 `upgrade head`，第二次必须是 no-op。"""
    engine = make_engine(tmp_path / "app.db")
    try:
        upgrade_to_head(engine)
        _seed_project(engine, "重复升级")
        names = _table_names(engine)
        before = _row_counts(engine, names)

        upgrade_to_head(engine)

        assert _table_names(engine) == names
        assert _row_counts(engine, names) == before
    finally:
        engine.dispose()
