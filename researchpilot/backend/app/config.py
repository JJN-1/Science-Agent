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
