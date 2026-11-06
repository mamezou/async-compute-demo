"""API 層。

POST /simulate        As-is：API サーバーの中で計算して、終わるまで返さない
POST /jobs            To-be：入力を置いてジョブを投入し、すぐ 202 を返す
GET  /jobs/{job_id}   状態確認。終わっていれば結果を返す
"""

from __future__ import annotations

import time
import uuid
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import backends, compute, metrics
from app import storage as st

app = FastAPI(title="async-compute-demo")
store = st.from_env()
backend = backends.from_env(store)


class SimulateRequest(BaseModel):
    n_paths: int = Field(100_000, ge=1, le=5_000_000)
    n_steps: int = Field(250, ge=1, le=1_000)
    n_assets: int = Field(20, ge=1, le=200)
    seed: int = 42
    method: Literal["naive", "vectorized"] = "vectorized"


class JobRequest(SimulateRequest):
    chunks: int = Field(8, ge=1, le=1_000, description="並列に動かすワーカー数")


def _params(req: SimulateRequest) -> compute.Params:
    return compute.Params(req.n_paths, req.n_steps, req.n_assets, req.seed)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


# async def にすると、この重い計算がイベントループを止めて他のリクエストも待たされる。
# def にすればスレッドプールで動くが、CPU を食い合うのは変わらない。
@app.post("/simulate")
def simulate(req: SimulateRequest) -> dict:
    t0 = time.perf_counter()
    summary = compute.simulate(_params(req), req.method)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    metrics.emit({"SyncDuration": (elapsed_ms, "Milliseconds")}, {"Phase": "sync"}, method=req.method)
    return {"summary": summary, "elapsed_ms": round(elapsed_ms)}


@app.post("/jobs", status_code=202)
def submit_job(req: JobRequest) -> dict:
    job_id = uuid.uuid4().hex[:12]
    params = _params(req)
    chunks = min(req.chunks, params.n_blocks)  # ブロック数より多く分けても意味がない
    st.put_json(
        store,
        st.input_key(job_id),
        {"params": params.to_dict(), "method": req.method, "submitted_at": time.time()},
    )
    backend.submit(job_id, chunks)
    return {"job_id": job_id, "chunks": chunks, "status_url": f"/jobs/{job_id}"}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    if not job_id.isalnum():
        raise HTTPException(400, "invalid job id")
    if store.exists(st.result_key(job_id)):
        result = st.get_json(store, st.result_key(job_id))
        turnaround = result["finished_at"] - result["submitted_at"]
        return {"status": "SUCCEEDED", "turnaround_s": round(turnaround, 2), **result}
    if not store.exists(st.meta_key(job_id)):
        raise HTTPException(404, "job not found")
    return {"job_id": job_id, **backend.status(job_id)}
