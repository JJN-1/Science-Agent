"""FIX-05：熔断状态与模型响应缓存的持久化回归。

此前的两个真问题：
1. 熔断计数只在进程内存里 —— 重启即清零，被熔断的后端马上要再挨一遍失败。
2. 缓存是 `LlmGateway` 里的进程内 dict，且键按**候选链首位**生成 ——
   降级到 B 的响应被记在 A 的键下，A 恢复后仍返回 B 的结果，直到进程重启。

单独成文件是因为这组测试都要跨「一次重建注册表/网关」来验证状态真的落盘了。
"""
from __future__ import annotations

import pytest

from app.ai.base import ChatMessage, CircuitOpen
from app.ai.budget import BudgetManager
from app.ai.client import LlmGateway
from app.ai.registry import CIRCUIT_KEY_PREFIX, ProviderRegistry
from app.ai.routing import RouteCandidate, Router
from app.store.dao import app_config as app_config_dao
from app.store.dao import llm_cache as llm_cache_dao
from app.store.dao import projects as projects_dao
from app.store.dao import runs as runs_dao

MESSAGES = [ChatMessage(role="user", content="同一个请求")]
TIER = "plan"


def _config(*, threshold: int = 3, cooldown: float = 300.0,
            cache: dict | None = None) -> dict:
    def mock(model: str, response: str, **extra) -> dict:
        return {"type": "mock", "model": model, "vendor": "mock",
                "capabilities": ["json_object"], "response": response,
                "price_per_1k": {"input": 0.0, "output": 0.0}, **extra}

    ai: dict = {
        "providers": {"a": mock("m1", '{"v": "A"}'), "b": mock("m2", '{"v": "B"}')},
        "circuit": {"failure_threshold": threshold, "cooldown_seconds": cooldown},
        "routing": {
            "extract": [{"provider": "a", "model": "m1"}],
            "plan": [{"provider": "a", "model": "m1"},
                     {"provider": "b", "model": "m2"}],
            "critique": [{"provider": "b", "model": "m2"}],
            "synthesize": [{"provider": "a", "model": "m1"}],
            "write": [{"provider": "a", "model": "m1"}],
        },
    }
    if cache is not None:
        ai["cache"] = cache
    return {"ai": ai}


def _gateway(cfg: dict, cache: dict | None = None) -> LlmGateway:
    registry = ProviderRegistry.from_config(cfg)
    router = Router.from_config(cfg, registry.providers_map())
    budget = BudgetManager(cfg["ai"].get("budget", {}))
    return LlmGateway(registry, router, budget, cache)


def _project_and_run(session):
    project = projects_dao.create(session, title="持久化", domain="cs-ai")
    run = runs_dao.create_run(session, project_id=project.id, stage_id="S1",
                              agent_id="scout")
    return project, run


def _call(gateway: LlmGateway, session, project, run):
    return gateway.call(
        session, project_id=project.id, run_id=run.id,
        stage_id="S1", agent_id="scout", tier=TIER, messages=MESSAGES,
    )


# ── 熔断状态 ─────────────────────────────────────

def test_circuit_state_survives_restart(session_factory):
    with session_factory() as session:
        reg = ProviderRegistry.from_config(_config())
        for _ in range(3):
            reg.record_failure("a", session)
        session.commit()
        assert app_config_dao.get(session, f"{CIRCUIT_KEY_PREFIX}a") == {
            "failures": 3, "opened_at": reg._circuits["a"].opened_at,
        }

    with session_factory() as session:
        fresh = ProviderRegistry.from_config(_config())  # 相当于进程重启
        fresh.load_circuits(session)
        assert fresh.health_report()[0]["circuit_failures"] == 3
        with pytest.raises(CircuitOpen):
            fresh.check_available("a", session)


def test_circuit_state_is_absent_before_load(session_factory):
    """对照：不回读时熔断状态为空——证明上一条测的确实是持久化而非内存巧合。"""
    with session_factory() as session:
        reg = ProviderRegistry.from_config(_config())
        for _ in range(3):
            reg.record_failure("a", session)
        session.commit()

    with session_factory() as session:
        fresh = ProviderRegistry.from_config(_config())
        fresh.check_available("a", session)  # 未回读 → 干净状态，不抛


def test_load_circuits_cleans_stale_provider_keys(session):
    """配置里已删掉的 provider，残留的熔断状态不该继续躺在 app_config 里。"""
    app_config_dao.put(session, f"{CIRCUIT_KEY_PREFIX}gone",
                       {"failures": 4, "opened_at": None})

    reg = ProviderRegistry.from_config(_config())
    reg.load_circuits(session)

    assert app_config_dao.get(session, f"{CIRCUIT_KEY_PREFIX}gone") is None
    assert reg.names() == ["a", "b"]


def test_circuit_success_removes_persisted_key(session):
    reg = ProviderRegistry.from_config(_config())
    reg.record_failure("a", session)
    assert app_config_dao.get(session, f"{CIRCUIT_KEY_PREFIX}a") is not None

    reg.record_success("a", session)
    assert app_config_dao.get(session, f"{CIRCUIT_KEY_PREFIX}a") is None


def test_circuit_cooldown_expiry_is_persisted(session):
    """冷却到期转半开这一步也是状态变更，必须落库，否则重启后又变成「刚熔断」。"""
    reg = ProviderRegistry.from_config(_config(cooldown=300.0))
    for _ in range(3):
        reg.record_failure("a", session)
    # 把开启时刻改到冷却期之外
    opened_at = reg._circuits["a"].opened_at
    app_config_dao.put(session, f"{CIRCUIT_KEY_PREFIX}a",
                       {"failures": 3, "opened_at": opened_at - 1000})

    fresh = ProviderRegistry.from_config(_config(cooldown=300.0))
    fresh.load_circuits(session)
    fresh.check_available("a", session)  # 半开：放行一次试探

    persisted = app_config_dao.get(session, f"{CIRCUIT_KEY_PREFIX}a")
    assert persisted["opened_at"] is None
    assert persisted["failures"] == 2  # 阈值 3 - 1


def test_reload_keeps_state_for_surviving_providers(session):
    reg = ProviderRegistry.from_config(_config())
    for _ in range(2):
        reg.record_failure("a", session)
    reg.record_failure("b", session)

    reg.reload(_config(), session)

    report = {row["name"]: row for row in reg.health_report()}
    assert report["a"]["circuit_failures"] == 2  # 内存状态未被重建清零
    assert report["b"]["circuit_failures"] == 1
    assert app_config_dao.get(session, f"{CIRCUIT_KEY_PREFIX}a")["failures"] == 2


def test_reload_drops_state_of_removed_provider(session):
    reg = ProviderRegistry.from_config(_config())
    for _ in range(2):
        reg.record_failure("b", session)
    assert app_config_dao.get(session, f"{CIRCUIT_KEY_PREFIX}b") is not None

    shrunk = _config()
    shrunk["ai"]["providers"].pop("b")
    shrunk["ai"]["routing"]["plan"] = [{"provider": "a", "model": "m1"}]
    shrunk["ai"]["routing"]["critique"] = [{"provider": "a", "model": "m1"}]
    reg.reload(shrunk, session)

    assert app_config_dao.get(session, f"{CIRCUIT_KEY_PREFIX}b") is None


# ── 响应缓存 ─────────────────────────────────────

def test_cache_hit_survives_gateway_restart(session_factory):
    with session_factory() as session:
        project, run = _project_and_run(session)
        first = _call(_gateway(_config()), session, project, run)
        assert first.provider == "a"
        session.commit()
        assert llm_cache_dao.count(session) == 1

    with session_factory() as session:
        project = projects_dao.list_all(session)[0]
        run = runs_dao.list_for_project(session, project.id)[0]
        gateway = _gateway(_config())
        second = _call(gateway, session, project, run)

        assert second.text == '{"v": "A"}'
        assert gateway.registry.get("a")._calls == 0  # 命中缓存，未真的调用模型
        assert runs_dao.list_steps(session, run.id)[-1].content["cached"] is True


def test_cache_writeback_uses_actual_responder_key(session):
    """D4 回归：降级到 b 作答时，结果必须记在 b 的键下，a 的键保持为空。"""
    project, run = _project_and_run(session)
    gateway = _gateway(_config())
    gateway.registry.get("a")._fail_times = 1  # 首候选先失败一次 → 降级到 b

    degraded = _call(gateway, session, project, run)
    assert degraded.provider == "b"

    key_a = gateway._cache_key(RouteCandidate("a", "m1"), MESSAGES, None, TIER)
    key_b = gateway._cache_key(RouteCandidate("b", "m2"), MESSAGES, None, TIER)
    assert llm_cache_dao.get(session, key_a) is None
    assert llm_cache_dao.get(session, key_b) is not None


def test_recovered_primary_is_not_blocked_by_degraded_cache(session):
    """D4 的后果修复：a 恢复后必须重新走 a，不能被 b 的旧缓存一直挡住。"""
    project, run = _project_and_run(session)
    gateway = _gateway(_config())
    provider_a = gateway.registry.get("a")
    provider_a._fail_times = 1

    assert _call(gateway, session, project, run).provider == "b"  # 降级作答
    assert provider_a._calls == 1

    recovered = _call(gateway, session, project, run)  # a 已恢复
    assert recovered.provider == "a"
    assert recovered.text == '{"v": "A"}'
    assert provider_a._calls == 2  # 真的重新调用了 a，而不是返回 b 的缓存


def test_expired_cache_is_not_returned(session):
    project, run = _project_and_run(session)
    gateway = _gateway(_config())
    key = gateway._cache_key(RouteCandidate("a", "m1"), MESSAGES, None, TIER)
    llm_cache_dao.put(session, cache_key=key, provider="a", model="m1",
                      tier=TIER, response={"text": "旧"}, ttl_seconds=-1)

    assert llm_cache_dao.get(session, key) is None
    assert llm_cache_dao.count(session) == 0  # 顺带删掉了过期行
    assert _call(gateway, session, project, run).text == '{"v": "A"}'


def test_cache_can_be_disabled(session):
    project, run = _project_and_run(session)
    gateway = _gateway(_config(), cache={"enabled": False})

    assert _call(gateway, session, project, run).provider == "a"
    assert _call(gateway, session, project, run).provider == "a"
    assert gateway.registry.get("a")._calls == 2
    assert llm_cache_dao.count(session) == 0


def test_purge_expired_removes_only_expired(session):
    llm_cache_dao.put(session, cache_key="keep", provider="a", model="m1",
                      tier=TIER, response={"text": "新"}, ttl_seconds=3600)
    llm_cache_dao.put(session, cache_key="drop", provider="a", model="m1",
                      tier=TIER, response={"text": "旧"}, ttl_seconds=-1)

    assert llm_cache_dao.purge_expired(session) == 1
    assert llm_cache_dao.get(session, "keep") is not None
