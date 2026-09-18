from __future__ import annotations

from app.store.dao import blackboard as bb_dao
from app.store.dao import projects as projects_dao
from app.store.dao import runs as runs_dao


def test_project_crud(session):
    project = projects_dao.create(session, title="图神经网络研究", goal="验证 GNN 在推荐系统的效果")
    assert project.id is not None
    assert project.status == "created"

    fetched = projects_dao.get(session, project.id)
    assert fetched is not None and fetched.title == "图神经网络研究"

    projects_dao.update(session, project.id, status="running")
    assert projects_dao.get(session, project.id).status == "running"

    assert len(projects_dao.list_all(session)) == 1


def test_blackboard_version_increments_per_type(session):
    project = projects_dao.create(session, title="p")
    first = bb_dao.write(session, project_id=project.id, obj_type="hypothesis",
                         payload={"text": "H1"}, produced_by="formalizer")
    second = bb_dao.write(session, project_id=project.id, obj_type="hypothesis",
                          payload={"text": "H2"}, produced_by="formalizer")
    other = bb_dao.write(session, project_id=project.id, obj_type="stage_output",
                         payload={}, produced_by="scout")
    assert (first.version, second.version, other.version) == (1, 2, 1)

    latest = bb_dao.latest_by_type(session, project.id)
    assert latest["hypothesis"].payload == {"text": "H2"}


def test_run_steps_sequence_and_finish(session):
    project = projects_dao.create(session, title="p")
    run = runs_dao.create_run(session, project_id=project.id, stage_id="S1", agent_id="scout")
    s1 = runs_dao.add_step(session, run_id=run.id, kind="thought", content={"text": "t1"})
    s2 = runs_dao.add_step(session, run_id=run.id, kind="result", content={"text": "r1"})
    assert (s1.seq, s2.seq) == (1, 2)

    runs_dao.finish_run(session, run_id=run.id, status="succeeded")
    reloaded = runs_dao.get_run(session, run.id)
    assert reloaded.status == "succeeded"
    assert reloaded.steps == 2
    assert reloaded.finished_at is not None
    assert [s.kind for s in runs_dao.list_steps(session, run.id)] == ["thought", "result"]
