"""CloudWatch のメトリクスを、ログ 1 行で送る（Embedded Metric Format）。

ECS / Batch のログを CloudWatch Logs に流しておけば、PutMetricData の権限も
SDK 呼び出しもなしに、このログからメトリクスが作られる。
"""

from __future__ import annotations

import json
import resource
import sys
import time

NAMESPACE = "AsyncComputeDemo"


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
