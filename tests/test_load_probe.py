from scripts.load_probe import percentiles, run_trial


def test_percentiles_use_nearest_rank_without_inventing_empty_values():
    assert percentiles([4, 1, 3, 2]) == {"count": 4, "p50_ms": 2, "p95_ms": 4}
    assert percentiles([]) == {"count": 0, "p50_ms": None, "p95_ms": None}


def test_small_real_http_probe_separates_commits_replays_and_conflicts(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    for mode in ["shared", "independent"]:
        report, rows = run_trial(tmp_path / mode, workers=2, requests=8, mode=mode, seed=7)
        assert report["passed"] is True
        assert report["unique_commits"] == 4
        assert report["outcomes"]["created"] == 4
        assert report["outcomes"]["capacity_conflict"] == 4
        assert report["outcomes"]["replayed"] >= 1
        assert report["invariants"]["booking_count"] == 4
        assert report["invariants"]["idempotency_count"] == 4
        assert report["invariants"]["booking_create_audit_count"] == 4
        assert all(row["latency_ms"] >= 0 for row in rows)
        assert len(rows) == 8 + report["outcomes"]["replayed"]
        assert not any("key" in row or "url" in row for row in rows)


def test_reconciliation_detects_booking_assigned_to_wrong_pool(db, service, product):
    from conftest import request

    from scripts.load_probe import reconcile

    other = service.create_product(
        "admin", sku="OTHER", name="Другой пул", category="Звук", capacity=3, daily_rate_cents=1000
    )
    booking = service.create_booking("load-operator", "probe-0", request(product["id"]))
    rows = [
        {
            "request_index": 0,
            "booking_id": booking["id"],
            "phase": "original",
            "outcome": "created",
            "product_id": other["id"],
        }
    ]
    checked = reconcile(db, [product, other], rows)
    assert "Бронь записана для другого пула оборудования" in checked["issues"]
