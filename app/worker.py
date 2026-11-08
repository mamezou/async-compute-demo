"""計算層のエントリポイント。AWS Batch のジョブとして動く。

  chunk  : 配列ジョブの 1 要素。担当ブロックを計算して途中結果を置く
  reduce : 配列ジョブが全部終わった後に 1 回だけ動き、途中結果を集約する

python -m app.worker chunk  --job-id ID --chunks 8 [--index 0]
python -m app.worker reduce --job-id ID --chunks 8
"""

from __future__ import annotations

import argparse
import os
import time

import numpy as np

from app import compute, metrics, storage as st


def run_chunk(store, job_id: str, index: int, n_chunks: int) -> dict:
    t0 = time.perf_counter()
    spec = st.get_json(store, st.input_key(job_id))
    params = compute.Params.from_dict(spec["params"])
    losses = compute.run_chunk(params, index, n_chunks, spec.get("method", "vectorized"))
    st.put_array(store, st.part_key(job_id, index), losses)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    metrics.emit(
        {
            "ChunkDuration": (elapsed_ms, "Milliseconds"),
            "PeakMemory": (metrics.peak_rss_mb(), "Megabytes"),
            "Paths": (losses.size, "Count"),
        },
        {"Phase": "chunk"},
        job_id=job_id,
        chunk=index,
        chunks=n_chunks,
    )
    return {"index": index, "paths": int(losses.size), "ms": elapsed_ms, "peak_mb": metrics.peak_rss_mb()}


def run_reduce(store, job_id: str, n_chunks: int) -> dict:
    t0 = time.perf_counter()
    parts = [st.get_array(store, st.part_key(job_id, i)) for i in range(n_chunks)]
    summary = compute.summarize(np.concatenate(parts))
    spec = st.get_json(store, st.input_key(job_id))
    result = {
        "job_id": job_id,
        "chunks": n_chunks,
        "params": spec["params"],
        "summary": summary,
        "submitted_at": spec.get("submitted_at"),
        "finished_at": time.time(),
    }
    st.put_json(store, st.result_key(job_id), result)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    total_s = result["finished_at"] - (spec.get("submitted_at") or result["finished_at"])
    metrics.emit(
        {
            "ReduceDuration": (elapsed_ms, "Milliseconds"),
            "JobTurnaround": (total_s * 1000, "Milliseconds"),
        },
        {"Phase": "reduce"},
        job_id=job_id,
        chunks=n_chunks,
    )
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["chunk", "reduce"])
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--chunks", type=int, required=True)
    ap.add_argument("--index", type=int, default=0)
    a = ap.parse_args()
    store = st.from_env()
    if a.phase == "chunk":
        # Batch の配列ジョブでは、要素番号がこの環境変数で渡される
        index = int(os.environ.get("AWS_BATCH_JOB_ARRAY_INDEX", a.index))
        run_chunk(store, a.job_id, index, a.chunks)
    else:
        run_reduce(store, a.job_id, a.chunks)


if __name__ == "__main__":
    main()
