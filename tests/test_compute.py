import time

import numpy as np
from fastapi.testclient import TestClient

from app import compute

SMALL = compute.Params(n_paths=12_000, n_steps=30, n_assets=5, seed=7)


def test_naive_and_vectorized_match():
    p = compute.Params(n_paths=300, n_steps=30, n_assets=5, seed=1)
    np.testing.assert_allclose(compute.run_chunk(p, method="naive"), compute.run_chunk(p, method="vectorized"), rtol=1e-12)


def test_split_does_not_change_result():
    whole = compute.run_chunk(SMALL)
    parts = np.concatenate([compute.run_chunk(SMALL, i, 3) for i in range(3)])
    np.testing.assert_array_equal(whole, parts)


def test_chunks_cover_all_blocks_once():
    blocks = sum((compute.chunk_blocks(SMALL, i, 4) for i in range(4)), [])
    assert blocks == list(range(SMALL.n_blocks))


def test_api_sync_and_async_agree(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BUCKET", raising=False)
    monkeypatch.delenv("BATCH_JOB_QUEUE", raising=False)
    import importlib

    from app import api

    importlib.reload(api)
    client = TestClient(api.app)
    body = {"n_paths": 12_000, "n_steps": 30, "n_assets": 5, "seed": 7}
    sync = client.post("/simulate", json=body).json()["summary"]
    job = client.post("/jobs", json={**body, "chunks": 3}).json()
    for _ in range(100):
        r = client.get(f"/jobs/{job['job_id']}").json()
        if r["status"] in ("SUCCEEDED", "FAILED"):
            break
        time.sleep(0.1)
    assert r["status"] == "SUCCEEDED", r
    assert r["summary"] == sync


def test_sub_batch_fits_memory_budget_at_max_input():
    p = compute.Params(n_paths=5_000, n_steps=1_000, n_assets=200)
    assert compute.sub_batch(p) * p.n_steps * p.n_assets * 8 <= compute.SUB_BATCH_BYTES


def test_sub_batch_size_does_not_change_result(monkeypatch):
    p = compute.Params(n_paths=3_000, n_steps=30, n_assets=5, seed=3)
    base = compute.run_chunk(p)
    monkeypatch.setattr(compute, "SUB_BATCH_BYTES", 30 * 5 * 8 * 7)  # 7 パスずつ
    np.testing.assert_allclose(compute.run_chunk(p), base, rtol=1e-12)


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BUCKET", raising=False)
    monkeypatch.delenv("BATCH_JOB_QUEUE", raising=False)
    import importlib

    from app import api

    importlib.reload(api)
    return api, TestClient(api.app)


def test_sync_rejects_large_input_and_bad_seed(tmp_path, monkeypatch):
    api, client = _client(tmp_path, monkeypatch)
    assert client.post("/simulate", json={"n_paths": 100_000, "n_steps": 1_000, "n_assets": 200}).status_code == 422
    assert client.post("/simulate", json={"n_paths": 100, "seed": -1}).status_code == 422
    # 同じ大きさでも /jobs の入力としては正しい（計算は走らせない）
    api.JobRequest(n_paths=100_000, n_steps=1_000, n_assets=200)


def test_sync_returns_429_when_busy(tmp_path, monkeypatch):
    api, client = _client(tmp_path, monkeypatch)
    assert api._sync_slots.acquire(blocking=False)  # 別のリクエストが計算中の状態を作る
    try:
        r = client.post("/simulate", json={"n_paths": 100})
        assert r.status_code == 429 and r.headers["retry-after"] == "5"
    finally:
        api._sync_slots.release()


def test_page_progress_and_metrics(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)
    page = client.get("/").text
    assert "同期 API と非同期ジョブ" in page
    assert client.get("/config").json()["on_aws"] is False
    job = client.post("/jobs", json={"n_paths": 12_000, "n_steps": 30, "n_assets": 5, "chunks": 3}).json()
    for _ in range(100):
        r = client.get(f"/jobs/{job['job_id']}").json()
        if r["status"] == "SUCCEEDED":
            break
        assert r["progress"]["total"] == 3
        time.sleep(0.1)
    assert r["status"] == "SUCCEEDED"
    series = client.get("/metrics").json()["series"]
    assert len(series["ChunkDuration"]) >= 3 and series["JobTurnaround"]
