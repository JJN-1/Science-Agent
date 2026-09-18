from __future__ import annotations

from app.config import ensure_data_dir, load_config


def test_first_start_creates_dirs_and_config(data_root):
    assert not data_root.exists()
    root = ensure_data_dir()
    for sub in ("files", "tex", "workspace", "packs", "logs"):
        assert (root / sub).is_dir()
    assert (root / "config.yaml").exists()


def test_load_config_default_and_user_override(data_root):
    # 默认配置生效
    config = load_config()
    assert config["app"]["host"] == "127.0.0.1"
    assert config["log"]["level"] == "INFO"
    # 用户配置覆盖同名键，其余键保留默认值
    ensure_data_dir(data_root)
    (data_root / "config.yaml").write_text("app:\n  port: 9999\n", encoding="utf-8")
    config = load_config()
    assert config["app"]["port"] == 9999
    assert config["app"]["host"] == "127.0.0.1"
