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
