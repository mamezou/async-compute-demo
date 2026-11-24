"""BatchBackend.status() が、子ジョブの失敗をすぐに FAILED と理由で返すことを、Batch の API を偽物にして確かめる。"""

from app import backends
from app import storage as st

OOM = "OutOfMemoryError: Container killed due to memory usage"


class FakeBatch:
    def __init__(self, jobs: list[dict], failed_children: list[dict] | None = None):
        self.jobs = jobs
        self.failed_children = failed_children or []
        self.list_calls = []

    def describe_jobs(self, jobs):
        return {"jobs": [j for j in self.jobs if j["jobId"] in jobs]}

    def list_jobs(self, **kwargs):
        self.list_calls.append(kwargs)
        return {"jobSummaryList": self.failed_children[: kwargs.get("maxResults", 100)]}


def _backend(tmp_path, fake, n_chunks):
    store = st.LocalStorage(str(tmp_path))
    b = backends.BatchBackend.__new__(backends.BatchBackend)  # boto3 を作らずに組み立てる
    b.store, b.queue, b.definition, b.batch = store, "q", "d", fake
    st.put_json(store, st.meta_key("j1"), {"backend": "batch", "chunk_job": "arr", "reduce_job": "red", "n_chunks": n_chunks})
    return b


def test_failed_child_reports_failed_with_oom_reason_while_others_run(tmp_path):
    # 8 チャンクのうち 1 つが OOM で落ち、ほかはまだ動いている。親は PENDING、集約は依存待ち
    fake = FakeBatch(
        jobs=[
            {"jobId": "arr", "status": "PENDING", "arrayProperties": {"statusSummary": {"RUNNING": 5, "SUCCEEDED": 2, "FAILED": 1}}},
            {"jobId": "red", "status": "PENDING"},
        ],
        failed_children=[
            {
                "jobId": "arr:3",
                "status": "FAILED",
                "statusReason": "Essential container in task exited",
                "arrayProperties": {"index": 3},
                "container": {"exitCode": 137, "reason": OOM},
            }
        ],
    )
    r = _backend(tmp_path, fake, 8).status("j1")
    assert r["status"] == "FAILED"
    assert r["progress"]["failed"] == 1 and r["progress"]["running"] == 5
    assert r["error"] == {
        "stage": "chunk",
        "batch_job_id": "arr:3",
        "index": 3,
        "status_reason": "Essential container in task exited",
        "exit_code": 137,
        "reason": OOM,
    }
    assert fake.list_calls == [{"arrayJobId": "arr", "jobStatus": "FAILED", "maxResults": 1}]


def test_single_chunk_failure_uses_the_job_itself(tmp_path):
    fake = FakeBatch(
        jobs=[
            {"jobId": "arr", "status": "FAILED", "statusReason": "Essential container in task exited", "container": {"exitCode": 137, "reason": OOM}},
            {"jobId": "red", "status": "PENDING"},
        ]
    )
    r = _backend(tmp_path, fake, 1).status("j1")
    assert r["status"] == "FAILED"
    assert r["error"]["stage"] == "chunk" and r["error"]["reason"] == OOM and r["error"]["exit_code"] == 137
    assert fake.list_calls == []


def test_reduce_failure_reports_reduce_reason(tmp_path):
    fake = FakeBatch(
        jobs=[
            {"jobId": "arr", "status": "SUCCEEDED", "arrayProperties": {"statusSummary": {"SUCCEEDED": 4}}},
            {"jobId": "red", "status": "FAILED", "statusReason": "Essential container in task exited", "container": {"exitCode": 1}},
        ]
    )
    r = _backend(tmp_path, fake, 4).status("j1")
    assert r["status"] == "FAILED"
    assert r["error"] == {
        "stage": "reduce",
        "batch_job_id": "red",
        "index": None,
        "status_reason": "Essential container in task exited",
        "exit_code": 1,
        "reason": "Essential container in task exited",
    }


def test_running_job_has_no_error(tmp_path):
    fake = FakeBatch(
        jobs=[
            {"jobId": "arr", "status": "PENDING", "arrayProperties": {"statusSummary": {"RUNNING": 3, "SUCCEEDED": 1}}},
            {"jobId": "red", "status": "PENDING"},
        ]
    )
    r = _backend(tmp_path, fake, 4).status("j1")
    assert r["status"] == "RUNNING" and "error" not in r
    assert fake.list_calls == []
