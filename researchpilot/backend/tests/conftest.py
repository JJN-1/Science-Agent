from __future__ import annotations

import pytest

from app.store.db import make_engine, make_session_factory
from app.store.migrations import upgrade_to_head


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setenv("RESEARCHPILOT_DATA_DIR", str(root))
    return root


@pytest.fixture
def engine(data_root):
    engine = make_engine(data_root / "app.db")
    upgrade_to_head(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture
def session(session_factory):
    s = session_factory()
    yield s
    s.rollback()
    s.close()
