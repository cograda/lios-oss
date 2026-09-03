"""`/api/integrations/{name}/detail` after a sync has actually happened.

Regression test for a bug that stood from 2026-07-26 to 2026-08-30: the
route read `SyncState` and `SyncHistory` attributes *after* its
`with db.session()` block had closed. The session commits on exit, which
expires the loaded instances, so the first attribute access raised
`DetachedInstanceError` and the endpoint 500'd.

🔑 Why it survived five weeks unnoticed, and what that dictates about this
test: the route's own null-guards (`if sync_state and ...`) short-circuit
for an integration that has never run, so nothing is ever dereferenced and
the endpoint returns 200. It therefore worked for exactly the integrations
that had done nothing, and failed for every one that had — and a test that
seeds no rows reproduces the healthy case, not the bug. **The seeding is
the test.**
"""

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _registered_integrations():
    """The route returns early with `{"error": ...}` for an unknown name, so
    without this the test exercises the not-found branch and never reaches the
    session handling it exists to check."""
    import app.integrations as integrations

    integrations.register_all()


@pytest.mark.anyio
async def test_detail_survives_a_synced_integration(db_session):
    from app.models.tokens import SyncHistory, SyncState
    from app.routes import integrations as route

    now = datetime.now(timezone.utc)
    db_session.add(SyncState(
        integration="weather",
        last_sync_at=now - timedelta(minutes=3),
        last_sync_status="ok",
        last_error=None,
        last_sync_duration_ms=412,
        consecutive_failures=0,
    ))
    db_session.add(SyncHistory(
        integration="weather",
        started_at=now - timedelta(minutes=3),
        status="ok",
        duration_ms=412,
        error=None,
        trigger="manual",
    ))
    db_session.commit()

    result = await route.integration_detail("weather")

    assert result["last_sync_status"] == "ok"
    assert result["last_sync_at"] is not None
    assert result["last_sync_duration_ms"] == 412
    assert result["consecutive_failures"] == 0
    assert len(result["history"]) == 1
    assert result["history"][0]["trigger"] == "manual"


@pytest.mark.anyio
async def test_detail_of_an_integration_that_has_never_run(db_session):
    """The case that always passed — kept so the guards stay honest."""
    from app.routes import integrations as route

    result = await route.integration_detail("weather")

    assert result["last_sync_status"] == "never"
    assert result["last_sync_at"] is None
    assert result["consecutive_failures"] == 0
    assert result["history"] == []
