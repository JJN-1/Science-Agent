"""migration 安全性：升级只加表，不动已有数据。

Sprint 3 一口气加了 5 张表（`budget_grants` / `app_config` / `llm_cache` / `jobs` /
`job_events`）。加表迁移最常见的翻车方式是顺手把已有表重建一遍 —— 数据没了而不自知。
这里用「升级前后逐表行数不变 + 新表确实出现」把它钉住。

另外 `upgrade head` 在每次启动时都会被无条件调用一次（零配置首启），所以
「重复执行是 no-op」也是必须成立的契约。
"""

from __future__ import annotations

from datetime import UTC, datetime

from alembic import command
from sqlalchemy import inspect, text

from app.store.dao import conversations as conversations_dao
from app.store.dao import messages as messages_dao
from app.store.dao import projects as projects_dao
from app.store.dao import task_plans as task_plans_dao
from app.store.dao import tool_calls as tool_calls_dao
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


# Sprint 4 · migration 5/6：conversations / messages、task_plans
BEFORE_CONVERSATIONS = "b1c4e7a92d38"
SPRINT4_TABLES = ("conversations", "messages")
BEFORE_TASK_PLANS = "3e7a5c91b4f2"
TASK_PLAN_TABLES = ("task_plans",)


def test_sprint4_migration_adds_task_plans_without_touching_rows(tmp_path):
    """结构化任务计划表同样只加表；已写入的会话/消息不能受影响。

    ``task_plans`` 有外键指向 ``conversations``，所以这条测试比 migration 5 多守一件事：
    **加外键不能把父表重建一遍** —— SQLite 上加 FK 常靠重建表实现，重建父表就等于
    把已有会话删光。这里先把会话写进去，再升到 head，最后确认它还在。
    """
    engine = make_engine(tmp_path / "app.db")
    try:
        command.upgrade(alembic_config(engine), BEFORE_TASK_PLANS)
        assert not (set(TASK_PLAN_TABLES) & _table_names(engine)), "旧版本上就已经有新表了"

        project_id = _seed_project(engine, "迁移前")
        session = make_session_factory(engine)()
        try:
            conversation = conversations_dao.create(
                session, project_id=project_id, title="迁移前的会话",
            )
            session.commit()
            conversation_id = conversation.id
        finally:
            session.close()

        # ⚠️ 这一步**必须**用裸 SQL，不能用 ``messages_dao.create``：
        # 当前 ORM 模型的 ``messages`` 已经带上 migration 7 新加的 ``tool_calls``，
        # 用它往「还没有那一列」的旧库上写，INSERT 会直接报「no such column」——
        # 失败的是「拿未来的形状写过去的库」这件事本身，而不是被测的迁移。
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO messages"
                    " (conversation_id, role, content, tokens, created_at)"
                    " VALUES (:cid, 'user', :content, 5, :now)"
                ),
                {
                    "cid": conversation_id, "content": "迁移前就说过的话",
                    "now": datetime.now(UTC),
                },
            )

        before = _row_counts(engine, _table_names(engine))

        upgrade_to_head(engine)

        assert set(TASK_PLAN_TABLES) <= _table_names(engine)
        # 老表行数一个都不能变 —— 尤其 conversations 不能因为加外键被重建
        assert _row_counts(engine, before.keys()) == before

        session = make_session_factory(engine)()
        try:
            assert conversations_dao.get(session, conversation_id).title == "迁移前的会话"
            kept = messages_dao.list_for_conversation(session, conversation_id)
            assert [m.content for m in kept] == ["迁移前就说过的话"]
            # 新增的可空列在**既有行上必须是 NULL**。它要是被设成 NOT NULL，
            # 迁移会在真实库上直接失败（既有行没有可填的值）——
            # 这条断言钉的就是「这一列确实是可空的」。
            assert kept[0].tool_calls is None

            plan = task_plans_dao.create(
                session, conversation_id=conversation_id,
                steps=[{"id": "s1", "title": "迁移后写入"}],
                mode="plan_execute", deterministic=True, seed=0,
            )
            session.commit()
            plan_id = plan.id
        finally:
            session.close()

        upgrade_to_head(engine)  # 启动路径的无条件调用

        session = make_session_factory(engine)()
        try:
            assert task_plans_dao.get(session, plan_id).steps[0]["title"] == "迁移后写入"
        finally:
            session.close()
    finally:
        engine.dispose()


def test_sprint4_migration_adds_conversation_tables_without_touching_rows(tmp_path):
    """Sprint 4 的加表迁移同样只加表；旧数据（含已写入的会话）必须原样存活。

    这里比已经升级到 head 的库再多验一层：**在新表有数据之后重跑迁移**，
    确认 conversations / messages 的内容不会被重建丢掉 ——
    加表迁移最阴的翻车方式就是「顺手重建一遍」。
    """
    engine = make_engine(tmp_path / "app.db")
    try:
        command.upgrade(alembic_config(engine), BEFORE_CONVERSATIONS)
        assert not (set(SPRINT4_TABLES) & _table_names(engine)), "旧版本上就已经有新表了"

        project_id = _seed_project(engine, "迁移前")

        upgrade_to_head(engine)
        assert set(SPRINT4_TABLES) <= _table_names(engine)

        session = make_session_factory(engine)()
        try:
            conversation = conversations_dao.create(
                session, project_id=project_id, title="迁移后写入",
            )
            messages_dao.create(
                session, conversation_id=conversation.id, role="user",
                content="不该被重建冲掉", tokens=7,
            )
            session.commit()
            conversation_id = conversation.id
        finally:
            session.close()

        upgrade_to_head(engine)

        session = make_session_factory(engine)()
        try:
            rows = messages_dao.list_for_conversation(session, conversation_id)
            assert [m.content for m in rows] == ["不该被重建冲掉"]
            assert conversations_dao.get(session, conversation_id).title == "迁移后写入"
        finally:
            session.close()
    finally:
        engine.dispose()


# Sprint 4 · migration 7：tool_calls 表 + messages.tool_calls 列
BEFORE_TOOL_CALLS = "8b2f6c04d1e9"
KERNEL_TABLES = ("tool_calls",)


def test_sprint4_migration_adds_tool_calls_and_message_column(tmp_path):
    """migration 7 同时做两件事：加 `tool_calls` 表、给 `messages` 加一列。

    «加列» 比 «加表» 多一类翻车方式：SQLite 上 ``ALTER TABLE ADD COLUMN`` 本身是安全的，
    但**列的可空性**错了就会在真实库上失败（既有行没有可填的值）。所以这里的断言分两层：
    升级后老消息还在、且它的新列是 NULL（可空），然后新写入才带得上工具调用。
    """
    engine = make_engine(tmp_path / "app.db")
    try:
        command.upgrade(alembic_config(engine), BEFORE_TOOL_CALLS)
        assert not (set(KERNEL_TABLES) & _table_names(engine)), "旧版本上就已经有新表了"
        assert "tool_calls" not in {
            col["name"] for col in inspect(engine).get_columns("messages")
        }, "旧版本的 messages 上不该有 tool_calls 列"

        project_id = _seed_project(engine, "迁移前")
        session = make_session_factory(engine)()
        try:
            conversation = conversations_dao.create(
                session, project_id=project_id, title="迁移前的会话",
            )
            session.commit()
            conversation_id = conversation.id
        finally:
            session.close()

        # 同上：旧库没有这一列，只能用裸 SQL 写「迁移前」的消息
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO messages"
                    " (conversation_id, role, content, tokens, created_at)"
                    " VALUES (:cid, 'user', :content, 5, :now)"
                ),
                {
                    "cid": conversation_id, "content": "迁移前就说过的话",
                    "now": datetime.now(UTC),
                },
            )
        before = _row_counts(engine, _table_names(engine))

        upgrade_to_head(engine)

        assert set(KERNEL_TABLES) <= _table_names(engine)
        assert "tool_calls" in {
            col["name"] for col in inspect(engine).get_columns("messages")
        }
        assert _row_counts(engine, before.keys()) == before

        session = make_session_factory(engine)()
        try:
            kept = messages_dao.list_for_conversation(session, conversation_id)
            assert [m.content for m in kept] == ["迁移前就说过的话"]
            assert kept[0].tool_calls is None, "新增列必须是可空的，否则既有行无法升级"

            # 新列真的可用：一对「assistant 带调用 + tool 给结果」要能落库并读回
            messages_dao.create(
                session, conversation_id=conversation_id, role="assistant", content="",
                tool_calls=[{"id": "c1", "name": "run_pipeline", "arguments": "{}"}],
                tokens=3,
            )
            messages_dao.create(
                session, conversation_id=conversation_id, role="tool", content='{"ok":true}',
                tool_call_id="c1", tokens=3,
            )
            audit = tool_calls_dao.create(
                session, conversation_id=conversation_id, run_id=None,
                tool_name="run_pipeline", args={"stage_ids": ["S1"]},
                permission="execute", status="ok", result={"run_ids": [1]},
                duration_ms=12,
            )
            session.commit()
            audit_id = audit.id
        finally:
            session.close()

        upgrade_to_head(engine)  # 启动路径的无条件调用

        session = make_session_factory(engine)()
        try:
            rows = messages_dao.list_for_conversation(session, conversation_id)
            assert rows[-2].tool_calls[0]["id"] == "c1"
            assert rows[-1].tool_call_id == "c1"
            assert tool_calls_dao.get(session, audit_id).args == {"stage_ids": ["S1"]}
            assert tool_calls_dao.count_for_conversation(session, conversation_id) == 1
        finally:
            session.close()
    finally:
        engine.dispose()
