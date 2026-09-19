from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path

import platformdirs
import yaml

APP_NAME = "ResearchPilot"
DATA_DIR_ENV = "RESEARCHPILOT_DATA_DIR"

SUBDIRS = ("files", "tex", "workspace", "packs", "logs")


def data_dir() -> Path:
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override)
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False, roaming=True))


def ensure_data_dir(root: Path | None = None) -> Path:
    """US-102：首次启动时自动创建数据目录结构与默认用户配置。"""
    root = root or data_dir()
    for sub in SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    user_config = root / "config.yaml"
    if not user_config.exists():
        user_config.write_text(
            "# ResearchPilot 用户配置（覆盖 config/default.yaml 中的同名键）\n",
            encoding="utf-8",
        )
    return root


def _deep_merge(base: dict, override: dict) -> dict:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return repo_root() / "config" / "default.yaml"


def load_config() -> dict:
    """加载配置：config/default.yaml 为底，用户 config.yaml 深度合并覆盖。"""
    config: dict = {}
    default_path = default_config_path()
    if default_path.exists():
        config = yaml.safe_load(default_path.read_text(encoding="utf-8")) or {}
    user_path = data_dir() / "config.yaml"
    if user_path.exists():
        user_cfg = yaml.safe_load(user_path.read_text(encoding="utf-8")) or {}
        config = _deep_merge(config, user_cfg)
    return config


# ── 用户配置写回（US-312：设置页接入自定义 Provider）────────────

def user_config_path() -> Path:
    return data_dir() / "config.yaml"


def read_user_config() -> dict:
    """只读用户 config.yaml（不含内置默认值）。"""
    path = user_config_path()
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def write_user_config(cfg: dict) -> None:
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def default_provider_names() -> set[str]:
    """定义在 config/default.yaml 里的 provider 名（内置项，不允许删除）。"""
    path = default_config_path()
    if not path.exists():
        return set()
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return set((cfg.get("ai", {}) or {}).get("providers", {}) or {})


def upsert_user_provider(name: str, provider_cfg: dict) -> None:
    cfg = read_user_config()
    providers = cfg.setdefault("ai", {}).setdefault("providers", {})
    providers[name] = provider_cfg
    write_user_config(cfg)


def remove_user_provider(name: str) -> bool:
    cfg = read_user_config()
    providers = (cfg.get("ai", {}) or {}).get("providers") or {}
    if name not in providers:
        return False
    del providers[name]
    write_user_config(cfg)
    return True


def upsert_user_routing(tier: str, candidates: list[dict]) -> None:
    """把档位路由写进用户配置。

    热切换只改内存的话，重启就回到默认路由 —— 用户刚接好的自定义模型会「自己掉了」。
    """
    cfg = read_user_config()
    routing = cfg.setdefault("ai", {}).setdefault("routing", {})
    routing[tier] = candidates
    write_user_config(cfg)
