"""性能比較とボトルネックの確認。

python -m bench.benchmark [--paths 20000] [--workers 1 2 4] [--profile]

各ケースを別プロセスで動かし、処理時間とピークメモリ（最大 RSS）を測る。
--profile を付けると、遅い実装を cProfile にかけて時間を食っている関数を出す。
"""

from __future__ import annotations

import argparse
import cProfile
import json
import os
import pstats
import subprocess
import sys

CASE = r"""
import json, resource, sys, time
from concurrent.futures import ProcessPoolExecutor
import numpy as np
from app import compute
p = compute.Params(n_paths={paths}, n_steps={steps}, n_assets={assets})
method, workers = "{method}", {workers}
t0 = time.perf_counter()
if workers == 1:
    losses = compute.run_chunk(p, 0, 1, method)
else:
    with ProcessPoolExecutor(workers) as ex:
        parts = ex.map(compute.run_chunk, [p] * workers, range(workers), [workers] * workers, [method] * workers)
        losses = np.concatenate(list(parts))
s = compute.summarize(losses)
ms = (time.perf_counter() - t0) * 1000
rss = max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss) / 1024
print(json.dumps({{"ms": ms, "rss_mb": rss, "var_99": s["var_99"]}}))
"""


def run_case(paths, steps, assets, method, workers) -> dict:
    code = CASE.format(paths=paths, steps=steps, assets=assets, method=method, workers=workers)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=os.getcwd())
    return json.loads(out.stdout.strip().splitlines()[-1])


def profile_naive(paths, steps, assets) -> None:
    from app import compute

    p = compute.Params(n_paths=paths, n_steps=steps, n_assets=assets)
    prof = cProfile.Profile()
    prof.runcall(compute.block_naive, p, 0)
    print(f"\n## cProfile: 遅い実装（{paths:,} パス）の上位 8 関数（累積時間順）")
    pstats.Stats(prof).sort_stats("cumulative").print_stats(8)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paths", type=int, default=20_000)
    ap.add_argument("--steps", type=int, default=250)
    ap.add_argument("--assets", type=int, default=20)
    ap.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4])
    ap.add_argument("--profile", action="store_true")
    a = ap.parse_args()

    cases = [("naive", 1)] + [("vectorized", w) for w in a.workers]
    print(f"# {a.paths:,} パス × {a.steps} 日 × {a.assets} 資産（CPU {os.cpu_count()} コア）\n")
    print("| 実装 | 並列数 | 処理時間 (秒) | 遅い実装比 | ピークメモリ (MB) | VaR99 |")
    print("| --- | ---: | ---: | ---: | ---: | ---: |")
    base = None
    for method, workers in cases:
        r = run_case(a.paths, a.steps, a.assets, method, workers)
        base = base or r["ms"]
        print(f"| {method} | {workers} | {r['ms'] / 1000:.2f} | {base / r['ms']:.1f}x | {r['rss_mb']:.0f} | {r['var_99']:.6f} |")
    if a.profile:
        profile_naive(min(a.paths, 2_000), a.steps, a.assets)


if __name__ == "__main__":
    main()
