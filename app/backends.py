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
        st.put_json(self.store, st.meta_key(job_id), {"backend": "local", "status": "RUNNING"})
        threading.Thread(target=self._run, args=(job_id, n_chunks), daemon=True).start()
        return {"backend": "local"}

    def _run(self, job_id: str, n_chunks: int) -> None:
        root = str(self.store.root)
        try:
            futures = [self.pool.submit(_local_chunk, root, job_id, i, n_chunks) for i in range(n_chunks)]
            for f in futures:
                f.result()
            worker.run_reduce(self.store, job_id, n_chunks)
        except Exception as e:  # noqa: BLE001  デモなので失敗内容をそのまま残す
            st.put_json(self.store, st.meta_key(job_id), {"backend": "local", "status": "FAILED", "error": repr(e)})

    def status(self, job_id: str) -> dict:
        return st.get_json(self.store, st.meta_key(job_id))


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
        meta = {"backend": "batch", "chunk_job": chunk_job, "reduce_job": reduce_job}
        st.put_json(self.store, st.meta_key(job_id), meta)
        return meta

    def status(self, job_id: str) -> dict:
        meta = st.get_json(self.store, st.meta_key(job_id))
        jobs = self.batch.describe_jobs(jobs=[meta["chunk_job"], meta["reduce_job"]])["jobs"]
        by_id = {j["jobId"]: j for j in jobs}
        chunk, reduce = by_id.get(meta["chunk_job"], {}), by_id.get(meta["reduce_job"], {})
        if "FAILED" in (chunk.get("status"), reduce.get("status")):
            status = "FAILED"
        elif chunk.get("status") in ("SUCCEEDED", "RUNNING") or reduce.get("status") == "RUNNING":
            status = "RUNNING"
        else:
            status = "QUEUED"
        return {
            **meta,
            "status": status,
            "chunks": chunk.get("arrayProperties", {}).get("statusSummary"),
        }


def from_env(store):
    queue = os.environ.get("BATCH_JOB_QUEUE")
    if queue:
        return BatchBackend(store, queue, os.environ["BATCH_JOB_DEFINITION"])
    return LocalBackend(store, int(os.environ.get("LOCAL_WORKERS", "0")) or None)
