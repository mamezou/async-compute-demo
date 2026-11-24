"""非同期ジョブの投入先。

LocalBackend : ノート PC で動かす用。プロセスプールで並列に計算する
BatchBackend : AWS Batch の配列ジョブ（並列計算）＋集約ジョブ（依存関係つき）

どちらも「入力を storage に置く → 計算層が読む → 結果を storage に置く」流れは同じ。
API は storage の result.json を見れば完了が分かる。
"""

from __future__ import annotations

import multiprocessing
import os
import threading
from concurrent.futures import ProcessPoolExecutor

import time

from app import metrics
from app import storage as st
from app import worker


def _local_chunk(root: str, job_id: str, index: int, n_chunks: int) -> dict:
    return worker.run_chunk(st.LocalStorage(root), job_id, index, n_chunks)


class LocalBackend:
    def __init__(self, store: st.LocalStorage, max_workers: int | None = None):
        self.store = store
        # API はスレッドを持つので fork ではなく spawn で子プロセスを作る
        self.pool = ProcessPoolExecutor(max_workers=max_workers, mp_context=multiprocessing.get_context("spawn"))

    def submit(self, job_id: str, n_chunks: int) -> dict:
        st.put_json(self.store, st.meta_key(job_id), {"backend": "local", "status": "RUNNING", "n_chunks": n_chunks})
        threading.Thread(target=self._run, args=(job_id, n_chunks), daemon=True).start()
        return {"backend": "local"}

    def _run(self, job_id: str, n_chunks: int) -> None:
        root = str(self.store.root)
        try:
            futures = [self.pool.submit(_local_chunk, root, job_id, i, n_chunks) for i in range(n_chunks)]
            for f in futures:
                r = f.result()
                # 子プロセスのメトリクスはここに届かないので、親で覚えておく（画面に出す用）
                metrics.record("ChunkDuration", r["ms"], "chunk")
                metrics.record("PeakMemory", r["peak_mb"], "chunk")
            worker.run_reduce(self.store, job_id, n_chunks)
        except Exception as e:  # noqa: BLE001  デモなので失敗内容をそのまま残す
            error = {"stage": "local", "reason": repr(e)}
            meta = {"backend": "local", "status": "FAILED", "n_chunks": n_chunks, "error": error}
            st.put_json(self.store, st.meta_key(job_id), meta)

    def status(self, job_id: str) -> dict:
        meta = st.get_json(self.store, st.meta_key(job_id))
        n = meta.get("n_chunks", 0)
        done = sum(self.store.exists(st.part_key(job_id, i)) for i in range(n))
        workers = self.pool._max_workers  # noqa: SLF001  同時に動くのはプールの大きさまで
        running = min(n - done, workers) if meta["status"] == "RUNNING" else 0
        return {**meta, "progress": {"total": n, "succeeded": done, "running": running, "waiting": n - done - running}}


class BatchBackend:
    def __init__(self, store, job_queue: str, job_definition: str):
        import boto3

        self.store = store
        self.queue = job_queue
        self.definition = job_definition
        self.batch = boto3.client("batch")

    def submit(self, job_id: str, n_chunks: int) -> dict:
        base = ["python", "-m", "app.worker"]
        args = ["--job-id", job_id, "--chunks", str(n_chunks)]
        chunk = {
            "jobName": f"chunk-{job_id}",
            "jobQueue": self.queue,
            "jobDefinition": self.definition,
            "containerOverrides": {"command": [*base, "chunk", *args]},
        }
        if n_chunks > 1:  # 配列ジョブは要素数 2 以上
            chunk["arrayProperties"] = {"size": n_chunks}
        chunk_job = self.batch.submit_job(**chunk)["jobId"]
        reduce_job = self.batch.submit_job(
            jobName=f"reduce-{job_id}",
            jobQueue=self.queue,
            jobDefinition=self.definition,
            dependsOn=[{"jobId": chunk_job}],  # 配列ジョブの全要素が成功してから動く
            containerOverrides={"command": [*base, "reduce", *args]},
        )["jobId"]
        meta = {"backend": "batch", "chunk_job": chunk_job, "reduce_job": reduce_job, "n_chunks": n_chunks}
        st.put_json(self.store, st.meta_key(job_id), meta)
        return meta

    def status(self, job_id: str) -> dict:
        meta = st.get_json(self.store, st.meta_key(job_id))
        jobs = self.batch.describe_jobs(jobs=[meta["chunk_job"], meta["reduce_job"]])["jobs"]
        by_id = {j["jobId"]: j for j in jobs}
        chunk, reduce = by_id.get(meta["chunk_job"], {}), by_id.get(meta["reduce_job"], {})
        n = meta.get("n_chunks", 1)
        # 配列ジョブの親は、子が動いている間も PENDING のまま。子の集計（statusSummary）を見る
        summary = chunk.get("arrayProperties", {}).get("statusSummary")
        if summary is None:  # 配列にしていない（chunks=1）ときは、ジョブ 1 つの状態をそのまま数える
            summary = {chunk.get("status", "SUBMITTED"): 1}
        progress = {
            "total": n,
            "succeeded": summary.get("SUCCEEDED", 0),
            "running": summary.get("RUNNING", 0) + summary.get("STARTING", 0),
            "failed": summary.get("FAILED", 0),
        }
        progress["waiting"] = n - progress["succeeded"] - progress["running"] - progress["failed"]
        error = None
        if "FAILED" in (chunk.get("status"), reduce.get("status")) or progress["failed"]:
            # 子ジョブが 1 つでも失敗したら、ほかの子ジョブの終了を待たずに FAILED を返す
            status = "FAILED"
            error = self._failure(meta, chunk, reduce, n)
        elif progress["running"] or progress["succeeded"] or reduce.get("status") in ("STARTING", "RUNNING"):
            status = "RUNNING"
        else:
            status = "QUEUED"
        result = {**meta, "status": status, "progress": progress, "reduce": reduce.get("status")}
        if error:
            result["error"] = error
        return result

    def _failure(self, meta: dict, chunk: dict, reduce: dict, n_chunks: int) -> dict:
        """失敗した理由。OOM なら reason に OutOfMemoryError が入る（PeakMemory は出ないので、ここで分かるようにする）。"""
        if n_chunks > 1:
            # 配列ジョブの親には理由が入らないので、失敗した子ジョブを 1 つ取って、その理由を返す
            failed = self.batch.list_jobs(arrayJobId=meta["chunk_job"], jobStatus="FAILED", maxResults=1)["jobSummaryList"]
            if failed:
                child = failed[0]
                return _error("chunk", child, child.get("arrayProperties", {}).get("index"))
        elif chunk.get("status") == "FAILED":
            return _error("chunk", chunk, 0)
        return _error("reduce", reduce, None)


def _error(stage: str, job: dict, index: int | None) -> dict:
    """DescribeJobs と ListJobs のどちらの形からでも、理由と終了コードを取り出す。"""
    container = job.get("container") or {}
    return {
        "stage": stage,
        "batch_job_id": job.get("jobId"),
        "index": index,
        "status_reason": job.get("statusReason"),
        "exit_code": container.get("exitCode"),
        "reason": container.get("reason") or job.get("statusReason"),
    }


def from_env(store):
    queue = os.environ.get("BATCH_JOB_QUEUE")
    if queue:
        return BatchBackend(store, queue, os.environ["BATCH_JOB_DEFINITION"])
    return LocalBackend(store, int(os.environ.get("LOCAL_WORKERS", "0")) or None)
