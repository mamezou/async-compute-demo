"""API 層。

POST /simulate        As-is：API サーバーの中で計算して、終わるまで返さない
POST /jobs            To-be：入力を置いてジョブを投入し、すぐ 202 を返す
GET  /jobs/{job_id}   状態確認。終わっていれば結果を返す
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from app import backends, compute, metrics
from app import storage as st

app = FastAPI(title="async-compute-demo")
store = st.from_env()
backend = backends.from_env(store)


class _Common(BaseModel):
    n_steps: int = Field(250, ge=1, le=1_000)
    n_assets: int = Field(20, ge=1, le=200)
    seed: int = Field(42, ge=0)  # NumPy の乱数の種は 0 以上
    method: Literal["naive", "vectorized"] = "vectorized"


# 同期で受ける計算量（パス数 × 日数 × 資産数）の上限。既定の 5 億は 10 万パス × 250 日 × 20 資産で、
# 4 コアの PC で約 9 秒。ALB のタイムアウト（60 秒）に余裕を持って収まる大きさにしておく。
SYNC_MAX_WORK = int(os.environ.get("SYNC_MAX_WORK", "500000000"))


class SimulateRequest(_Common):
    n_paths: int = Field(100_000, ge=1, le=5_000_000)

    @model_validator(mode="after")
    def _fits_sync(self) -> "SimulateRequest":
        if self.n_paths * self.n_steps * self.n_assets > SYNC_MAX_WORK:
            raise ValueError(f"too large for a synchronous call (n_paths * n_steps * n_assets > {SYNC_MAX_WORK}); use POST /jobs")
        return self


class JobRequest(_Common):
    n_paths: int = Field(100_000, ge=1, le=5_000_000)
    chunks: int = Field(8, ge=1, le=1_000, description="並列に動かすワーカー数")


# 同期計算を同時にいくつまで受けるか。超えた分は待たせずに 429 を返す。
_sync_slots = threading.BoundedSemaphore(int(os.environ.get("SYNC_MAX_CONCURRENCY", "1")))


def _params(req: SimulateRequest | JobRequest) -> compute.Params:
    return compute.Params(req.n_paths, req.n_steps, req.n_assets, req.seed)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


_STATIC = Path(__file__).with_name("static")
_PAGE = _STATIC / "index.html"
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def page() -> str:
    """同期と非同期を並べて動かし、進み具合と CloudWatch の数字を見る画面。"""
    return _PAGE.read_text(encoding="utf-8")


@app.get("/config")
def config() -> dict:
    """画面が使う設定。上限と、AWS のコンソールへのリンク。"""
    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    console = f"https://{region}.console.aws.amazon.com"
    dashboard, queue = os.environ.get("DASHBOARD_NAME"), os.environ.get("BATCH_JOB_QUEUE")
    return {
        "sync_max_work": SYNC_MAX_WORK,
        "on_aws": bool(queue),
        "dashboard_url": f"{console}/cloudwatch/home?region={region}#dashboards:name={dashboard}" if dashboard else None,
        "batch_jobs_url": f"{console}/batch/home?region={region}#jobs" if queue else None,
    }


@app.get("/metrics")
def recent_metrics(minutes: int = Query(60, ge=5, le=180)) -> dict:
    return metrics.recent(minutes)


# 同期 API の限界を示すエンドポイント。
# - async def にすると、計算中はイベントループが止まり、/health を含む他のリクエストが全部待たされる。
# - def にするとスレッドプール（既定 40 本）で動くのでイベントループは止まらない。ただし CPU は
#   食い合い、重いリクエストが重なるとスレッドを使い切る。ALB が 60 秒で接続を切っても計算は続く。
# そこで同時実行数とパス数に上限をかけ、超えたら 429 を返す。これ以上の計算は /jobs に回す。
@app.post("/simulate")
def simulate(req: SimulateRequest) -> dict:
    if not _sync_slots.acquire(blocking=False):
        raise HTTPException(429, "busy: use POST /jobs", headers={"Retry-After": "5"})
    try:
        t0 = time.perf_counter()
        summary = compute.simulate(_params(req), req.method)
    finally:
        _sync_slots.release()
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
