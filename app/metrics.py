"""CloudWatch のメトリクスを、ログ 1 行で送る（Embedded Metric Format）。

ECS / Batch のログを CloudWatch Logs に流しておけば、PutMetricData の権限も
SDK 呼び出しもなしに、このログからメトリクスが作られる。
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time
from collections import deque

NAMESPACE = "AsyncComputeDemo"

# 画面に出すメトリクス。（名前, Phase）
SHOWN = [
    ("SyncDuration", "sync"),
    ("ChunkDuration", "chunk"),
    ("PeakMemory", "chunk"),
    ("JobTurnaround", "reduce"),
]

# ローカルで動かすとき用に、このプロセスで出した値を覚えておく（AWS では CloudWatch から読む）
_recent: deque = deque(maxlen=2_000)


def record(name: str, value: float, phase: str, at: float | None = None) -> None:
    _recent.append((at or time.time(), name, phase, float(value)))


def peak_rss_mb() -> float:
    # Linux では KB 単位
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def emit(metrics: dict[str, tuple[float, str]], dimensions: dict[str, str], **properties) -> None:
    """metrics は {名前: (値, 単位)}。単位は Milliseconds, Megabytes, Count など。"""
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [list(dimensions)],
                    "Metrics": [{"Name": k, "Unit": unit} for k, (_, unit) in metrics.items()],
                }
            ],
        },
        **dimensions,
        **{k: v for k, (v, _) in metrics.items()},
        **properties,
    }
    print(json.dumps(record), file=sys.stdout, flush=True)
    phase = dimensions.get("Phase", "")
    for k, (v, _) in metrics.items():
        _recent.append((time.time(), k, phase, float(v)))


def recent(minutes: int = 60) -> dict:
    """直近 minutes 分の値。{名前: [[UNIX 秒, 値], ...]}。

    AWS（BATCH_JOB_QUEUE あり）では CloudWatch の 1 分ごとの最大値、ローカルでは 1 件ずつの値。
    """
    if os.environ.get("BATCH_JOB_QUEUE"):
        return {"source": "cloudwatch", "series": _from_cloudwatch(minutes)}
    since = time.time() - minutes * 60
    series = {name: [] for name, _ in SHOWN}
    for t, name, phase, v in _recent:
        if t >= since and (name, phase) in SHOWN:
            series[name].append([t, v])
    return {"source": "local", "series": series}


def _from_cloudwatch(minutes: int) -> dict:
    import datetime as dt

    import boto3

    end = dt.datetime.now(dt.timezone.utc)
    queries = [
        {
            "Id": f"m{i}",
            "Label": name,
            "MetricStat": {
                "Metric": {"Namespace": NAMESPACE, "MetricName": name, "Dimensions": [{"Name": "Phase", "Value": phase}]},
                "Period": 60,
                "Stat": "Maximum",
            },
        }
        for i, (name, phase) in enumerate(SHOWN)
    ]
    res = boto3.client("cloudwatch").get_metric_data(
        MetricDataQueries=queries, StartTime=end - dt.timedelta(minutes=minutes), EndTime=end, ScanBy="TimestampAscending"
    )
    return {r["Label"]: [[t.timestamp(), v] for t, v in zip(r["Timestamps"], r["Values"])] for r in res["MetricDataResults"]}
