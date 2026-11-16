from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from echo.runtime.api import create_app
from echo.runtime.lease import LeaseConflict, LeaseStore, PoolEmpty
from echo.runtime.models import Lease

REPO = Path(__file__).resolve().parent.parent
PROCESS = "loan_payoff_v3"
APPS = {PROCESS: {"app1": "http://127.0.0.1:8101/", "app2": "http://127.0.0.1:8102/"}}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 15, 14, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def make_store(tmp_path: Path, n: int = 1, **kw) -> LeaseStore:
    store = LeaseStore(tmp_path / "leases.db", **kw)
    keys = ["4471", "4472", "4473", "4474", "4475", "4476", "4477", "4478"][:n]
    store.register_pool(PROCESS, "B17", [("customer", k, None) for k in keys])
    return store


def run_concurrently(fns: list) -> list:
    barrier = threading.Barrier(len(fns))
    results: list = [None] * len(fns)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            results[i] = fns[i]()
        except Exception as exc:
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(fns))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def test_lease_example_in_specs_validates() -> None:
    data = json.loads((REPO / "specs" / "lease.example.json").read_text(encoding="utf-8"))
    lease = Lease.model_validate(data)
    assert lease.entities[0].key == "4471"
    assert lease.state == "leased"


def test_lease_example_from_docs_validates() -> None:
    Lease.model_validate(
        {
            "lease_id": "lease_7f3a",
            "process": "loan_payoff_v3",
            "baseline": "B17",
            "entities": [{"entity": "customer", "key": "4471"}],
            "holder": "agent-team-a/run-104",
            "issued": "…",
            "ttl_minutes": 30,
            "state": "leased",
        }
    )


def test_two_concurrent_threads_cannot_lease_the_same_customer(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    results = run_concurrently(
        [lambda h=h: store.request(PROCESS, h, key="4471") for h in ("team-a", "team-b")]
    )
    leases = [r for r in results if isinstance(r, Lease)]
    conflicts = [r for r in results if isinstance(r, LeaseConflict)]
    assert len(leases) == 1 and len(conflicts) == 1
    assert conflicts[0].leased_by == leases[0].holder
    assert conflicts[0].expires == leases[0].expires


def test_two_store_instances_on_one_file_cannot_lease_the_same_customer(tmp_path: Path) -> None:
    make_store(tmp_path)
    a = LeaseStore(tmp_path / "leases.db")
    b = LeaseStore(tmp_path / "leases.db")
    results = run_concurrently(
        [lambda: a.request(PROCESS, "team-a"), lambda: b.request(PROCESS, "team-b")]
    )
    leases = [r for r in results if isinstance(r, Lease)]
    empties = [r for r in results if isinstance(r, PoolEmpty)]
    assert len(leases) == 1 and len(empties) == 1


def test_many_concurrent_requests_get_distinct_customers(tmp_path: Path) -> None:
    store = make_store(tmp_path, n=4)
    stores = [LeaseStore(tmp_path / "leases.db") for _ in range(8)]
    results = run_concurrently(
        [lambda s=s, i=i: s.request(PROCESS, f"h{i}") for i, s in enumerate(stores)]
    )
    keys = [r.entities[0].key for r in results if isinstance(r, Lease)]
    assert sorted(keys) == ["4471", "4472", "4473", "4474"]
    assert sum(isinstance(r, PoolEmpty) for r in results) == 4
    assert store.leased_keys(PROCESS) == set(keys)


def test_concurrent_http_requests_one_gets_201_other_409_naming_holder(tmp_path: Path) -> None:
    client = TestClient(create_app(make_store(tmp_path), apps=APPS))
    results = run_concurrently(
        [
            lambda h=h: client.post(
                f"/process/{PROCESS}/request", json={"holder": h, "key": "4471"}
            )
            for h in ("team-a", "team-b")
        ]
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [201, 409]
    ok = next(r for r in results if r.status_code == 201).json()
    conflict = next(r for r in results if r.status_code == 409).json()
    holder = client.get(f"/lease/{ok['lease_id']}").json()["holder"]
    assert conflict == {"leased_by": holder, "expires": ok["expires"]}


def test_request_returns_lease_shape_with_echo_urls(tmp_path: Path) -> None:
    client = TestClient(create_app(make_store(tmp_path), apps=APPS))
    r = client.post(f"/process/{PROCESS}/request", json={"holder": "team-a", "ttl_minutes": 5})
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"lease_id", "baseline", "entities", "apps", "expires"}
    assert body["baseline"] == "B17"
    assert body["entities"] == [{"entity": "customer", "key": "4471", "masked_key": None}]
    assert body["apps"] == [
        {"app": "app1", "url": "http://127.0.0.1:8101/"},
        {"app": "app2", "url": "http://127.0.0.1:8102/"},
    ]
    assert body["expires"].endswith("Z")
    lease = client.get(f"/lease/{body['lease_id']}").json()
    assert lease["state"] == "leased" and lease["ttl_minutes"] == 5


def test_request_404_when_pool_empty_or_process_unknown(tmp_path: Path) -> None:
    client = TestClient(create_app(make_store(tmp_path)))
    assert client.post(f"/process/{PROCESS}/request", json={"holder": "a"}).status_code == 201
    assert client.post(f"/process/{PROCESS}/request", json={"holder": "b"}).status_code == 404
    assert client.post("/process/nope/request", json={"holder": "b"}).status_code == 404
    assert client.get("/lease/lease_missing").status_code == 404
    assert client.get("/process/nope/readiness").status_code == 404


def test_release_runs_reset_hook_and_returns_customer_to_pool(tmp_path: Path) -> None:
    seen: list[Lease] = []

    def hook(lease: Lease) -> bool:
        seen.append(lease)
        return True

    store = make_store(tmp_path, reset_hook=hook)
    client = TestClient(create_app(store))
    first = client.post(f"/process/{PROCESS}/request", json={"holder": "team-a"}).json()
    store.mark_running(first["lease_id"])
    r = client.post(f"/lease/{first['lease_id']}/release", json={"outcome": "pass"})
    assert r.status_code == 200 and r.json() == {"reset": "ok"}
    assert len(seen) == 1 and seen[0].state == "resetting" and seen[0].outcome == "pass"
    assert store.get(first["lease_id"]).state == "available"
    again = client.post(f"/process/{PROCESS}/request", json={"holder": "team-b", "key": "4471"})
    assert again.status_code == 201
    # A repeated release reports the recorded result and does not rerun the hook.
    assert store.release(first["lease_id"], "pass") == {"reset": "ok"}
    assert len(seen) == 1


def test_release_rejects_unknown_outcome(tmp_path: Path) -> None:
    client = TestClient(create_app(make_store(tmp_path)))
    lease = client.post(f"/process/{PROCESS}/request", json={"holder": "a"}).json()
    assert (
        client.post(f"/lease/{lease['lease_id']}/release", json={"outcome": "ok"}).status_code
        == 422
    )
    assert client.post("/lease/lease_missing/release", json={"outcome": "pass"}).status_code == 404


@pytest.mark.parametrize("fails", ["false", "raises"])
def test_failing_reset_hook_quarantines_the_customer(tmp_path: Path, fails: str) -> None:
    def hook(lease: Lease) -> bool:
        if fails == "raises":
            raise RuntimeError("could not verify reset")
        return False

    store = make_store(tmp_path, reset_hook=hook)
    client = TestClient(create_app(store))
    lease = client.post(f"/process/{PROCESS}/request", json={"holder": "a"}).json()
    r = client.post(f"/lease/{lease['lease_id']}/release", json={"outcome": "fail"})
    assert r.json() == {"reset": "quarantined"}
    assert store.get(lease["lease_id"]).state == "quarantined"
    assert store.get(lease["lease_id"]).outcome == "fail"
    with pytest.raises(PoolEmpty):
        store.request(PROCESS, "b", key="4471")
    assert store.readiness(PROCESS)["pool_quarantined"] == 1


def test_expired_lease_is_quarantined_not_reused(tmp_path: Path) -> None:
    clock = Clock()
    store = make_store(tmp_path, n=2, clock=clock)
    lease = store.request(PROCESS, "a", ttl_minutes=10, key="4471")
    clock.now += timedelta(minutes=5)
    assert store.expire() == []
    clock.now += timedelta(minutes=6)
    assert store.expire() == [lease.lease_id]
    assert store.get(lease.lease_id).state == "quarantined"
    assert store.leased_keys(PROCESS) == set()
    with pytest.raises(PoolEmpty):
        store.request(PROCESS, "b", key="4471")
    assert store.request(PROCESS, "b").entities[0].key == "4472"
    assert store.release(lease.lease_id, "abandoned") == {"reset": "quarantined"}


def test_request_expires_stale_leases_before_reporting_a_conflict(tmp_path: Path) -> None:
    clock = Clock()
    store = make_store(tmp_path, clock=clock)
    store.request(PROCESS, "a", ttl_minutes=1)
    with pytest.raises(LeaseConflict):
        store.request(PROCESS, "b", key="4471")
    clock.now += timedelta(minutes=2)
    with pytest.raises(PoolEmpty):
        store.request(PROCESS, "b", key="4471")


def test_readiness_counts_pool_states(tmp_path: Path) -> None:
    store = make_store(tmp_path, n=4, reset_hook=lambda lease: lease.entities[0].key != "4472")
    client = TestClient(create_app(store, apps=APPS, readiness_extra=lambda p: {"checked": p}))
    a = store.request(PROCESS, "a")
    b = store.request(PROCESS, "b")
    store.request(PROCESS, "c")
    store.release(a.lease_id, "pass")
    store.release(b.lease_id, "fail")
    body = client.get(f"/process/{PROCESS}/readiness").json()
    assert body["baseline"] == "B17"
    assert body["last_loaded"].endswith("Z")
    assert body["apps_built"] == ["app1", "app2"]
    assert (body["pool_available"], body["pool_leased"], body["pool_quarantined"]) == (2, 1, 1)
    assert body["checked"] == PROCESS


def test_register_pool_is_idempotent_and_keeps_leased_rows(tmp_path: Path) -> None:
    store = make_store(tmp_path, n=2)
    lease = store.request(PROCESS, "a", key="4471")
    added = store.register_pool(
        PROCESS,
        "B18",
        [("customer", "4471", "M1"), ("customer", "4472", "M2"), ("customer", "4473", None)],
    )
    assert added == 1
    assert store.get(lease.lease_id).state == "leased"
    assert store.leased_keys(PROCESS) == {"4471"}
    with pytest.raises(LeaseConflict):
        store.request(PROCESS, "b", key="4471")
    held = store.get(lease.lease_id)
    assert held.baseline == "B17" and held.entities[0].masked_key is None
    other = store.request(PROCESS, "b", key="4472")
    assert other.baseline == "B18" and other.entities[0].masked_key == "M2"
    assert store.readiness(PROCESS)["pool_available"] == 1


def test_set_baseline_moves_only_non_leased_rows(tmp_path: Path) -> None:
    store = make_store(tmp_path, n=3)
    lease = store.request(PROCESS, "a", key="4471")
    moved = store.set_baseline(PROCESS, "B18", None)
    assert sorted(moved) == ["4472", "4473"]
    assert store.get(lease.lease_id).baseline == "B17"
    with pytest.raises(PoolEmpty):
        store.request(PROCESS, "b", baseline="B17")
    assert store.request(PROCESS, "b", baseline="B18").baseline == "B18"
    # After release the reset customer comes back on the current baseline.
    store.release(lease.lease_id, "pass")
    assert store.request(PROCESS, "c", key="4471").baseline == "B18"
