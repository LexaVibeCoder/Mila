"""Retention of sleep staging rows (TODO.md S17).

Applied rows older than 30 days are collected; recent applied rows and any
unapplied row (needed by recovery) are never collected.
"""

import core.sleep.manager as sm
from core import db as mila_db
from core.sleep.state import _ensure_sleep_tables, gc_staged_operations, get_state, SleepState


def _insert_staging(target_id, applied, age_days, session_id="session-old"):
    _ensure_sleep_tables()
    conn = mila_db.get_conn()
    try:
        conn.execute(
            "INSERT INTO sleep_transaction_staging "
            "(session_id, operation_type, target_table, target_id, payload, created_at, applied) "
            "VALUES (?, 'insert', 'memories', ?, '{}', datetime('now', ?), ?)",
            (session_id, target_id, f"-{age_days} days", applied),
        )
        conn.commit()
    finally:
        conn.close()


def _remaining_targets(env):
    return {r["target_id"] for r in env._rows(
        "SELECT target_id FROM sleep_transaction_staging")}


def _old_applied_count(env):
    return env._rows(
        "SELECT COUNT(*) AS n FROM sleep_transaction_staging "
        "WHERE applied=1 AND created_at < datetime('now', '-30 days')")[0]["n"]


def test_gc_removes_only_old_applied_rows(sleep_env):
    env = sleep_env
    _insert_staging("stale-applied", applied=1, age_days=31)
    _insert_staging("recent-applied", applied=1, age_days=1)
    _insert_staging("old-pending", applied=0, age_days=60)

    removed = gc_staged_operations()

    assert removed == 1
    assert _remaining_targets(env) == {"recent-applied", "old-pending"}


def test_gc_retention_boundary_keeps_rows_just_inside_thirty_days(sleep_env):
    env = sleep_env
    _insert_staging("inside-window", applied=1, age_days=29)
    _insert_staging("outside-window", applied=1, age_days=31)

    gc_staged_operations()

    assert _remaining_targets(env) == {"inside-window"}


async def test_commit_collects_old_applied_rows(sleep_env):
    env = sleep_env
    _insert_staging("stale-applied", applied=1, age_days=40)
    _insert_staging("recent-applied", applied=1, age_days=2)
    session = env.begin_sleep()
    await env.stage_fact(session, content="alpha fact")

    assert await sm.commit_all(session) is True

    targets = _remaining_targets(env)
    assert "stale-applied" not in targets
    assert "recent-applied" in targets
    assert get_state() == SleepState.AWAKE


async def test_applied_staging_stays_bounded_across_repeated_sleeps(sleep_env):
    env = sleep_env
    for cycle in range(3):
        for i in range(5):
            _insert_staging(f"stale-{cycle}-{i}", applied=1, age_days=45)
        session = env.begin_sleep()
        await env.stage_fact(session, content=f"alpha fact {cycle}")
        assert await sm.commit_all(session) is True

    remaining = _remaining_targets(env)
    assert remaining, "the sleeps' own applied rows must still be there"
    assert _old_applied_count(env) == 0
    assert all(not t.startswith("stale-") for t in remaining)
