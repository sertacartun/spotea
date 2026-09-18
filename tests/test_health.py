"""/health, and the background loop surviving a failed cycle."""

import asyncio

from app import scheduler
from app.version import APP_VERSION


def test_health_reports_ok_while_everything_runs(client):
    res = client.get("/health")

    assert res.status_code == 200
    assert res.json() == {
        "status": "ok",
        "database": True,
        "scheduler": True,
        "version": APP_VERSION,
    }


def test_health_is_503_when_the_refresh_loop_has_died(client, monkeypatch):
    """Reporting it is what lets compose's restart policy replace the process."""
    monkeypatch.setattr(scheduler, "is_alive", lambda: False)

    res = client.get("/health")

    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "degraded"
    assert body["scheduler"] is False
    assert body["database"] is True


def test_health_is_503_when_the_database_is_unreachable(client, monkeypatch):
    def broken_session():
        raise RuntimeError("unable to open database file")

    monkeypatch.setattr("app.main.SessionLocal", broken_session)

    res = client.get("/health")

    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "degraded"
    assert body["database"] is False


def test_the_background_loop_survives_a_failure_during_a_cycle(monkeypatch):
    calls: list[int] = []

    def sometimes_locked() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")

    monkeypatch.setattr(scheduler, "_sweep_disk", sometimes_locked)
    monkeypatch.setattr(scheduler, "ERROR_BACKOFF_SECONDS", 0)

    async def drive() -> tuple[bool, int]:
        scheduler.start()
        try:
            for _ in range(200):
                await asyncio.sleep(0.005)
                if len(calls) >= 2:
                    break
            return scheduler.is_alive(), len(calls)
        finally:
            await scheduler.stop()

    alive, attempts = asyncio.run(drive())

    assert attempts >= 2, "the loop did not come back round after the failure"
    assert alive, "the loop died on an exception raised during a cycle"
