"""CPU・メモリ負荷の高い計算のサンプル。

モンテカルロ法でポートフォリオの損失分布を求め、VaR / CVaR を返す。
同じ入力から、遅い実装（pandas で 1 パスずつ）と速い実装（NumPy でまとめて）が
同じ結果を出すようにしてある。分割して並列に計算しても結果は変わらない。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

# 乱数はブロック単位で生成する。ブロック b は seed と b から決まるので、
# どのワーカーがどのブロックを担当しても結果が同じになる。
BLOCK_SIZE = 5_000
# 速い実装で一度に展開するパス数。メモリ使用量はおよそ
# SUB_BATCH * n_steps * n_assets * 8 バイト（既定値で 40MB）。
SUB_BATCH = 1_000


@dataclass(frozen=True)
class Params:
    n_paths: int = 100_000
    n_steps: int = 250
    n_assets: int = 20
    seed: int = 42

    @classmethod
    def from_dict(cls, d: dict) -> "Params":
        return cls(**{k: int(d[k]) for k in ("n_paths", "n_steps", "n_assets", "seed") if k in d})

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def n_blocks(self) -> int:
        return -(-self.n_paths // BLOCK_SIZE)


def market(p: Params) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """資産ごとの日次の期待リターン、ボラティリティ、ポートフォリオの比率。"""
    rng = np.random.default_rng(p.seed)
    mu = rng.uniform(0.0, 0.10, p.n_assets) / 250
    sigma = rng.uniform(0.10, 0.40, p.n_assets) / np.sqrt(250)
    weights = rng.dirichlet(np.ones(p.n_assets))
    return mu, sigma, weights


def _block_paths(p: Params, b: int) -> int:
    return min(BLOCK_SIZE, p.n_paths - b * BLOCK_SIZE)


def block_naive(p: Params, b: int) -> np.ndarray:
    """As-is 想定の実装。1 パスずつ DataFrame を作るので遅い。"""
    mu, sigma, w = market(p)
    rng = np.random.default_rng([p.seed, b])
    losses = np.empty(_block_paths(p, b))
    for i in range(losses.size):
        z = rng.standard_normal((p.n_steps, p.n_assets))
        returns = pd.DataFrame(mu + sigma * z)
        growth = (1.0 + returns).prod(axis=0).to_numpy()
        losses[i] = 1.0 - float(growth @ w)
    return losses


def block_vectorized(p: Params, b: int) -> np.ndarray:
    """チューニング後の実装。SUB_BATCH 本ずつまとめて NumPy で計算する。"""
    mu, sigma, w = market(p)
    rng = np.random.default_rng([p.seed, b])
    n = _block_paths(p, b)
    losses = np.empty(n)
    for start in range(0, n, SUB_BATCH):
        m = min(SUB_BATCH, n - start)
        z = rng.standard_normal((m, p.n_steps, p.n_assets))
        z *= sigma
        z += 1.0 + mu
        growth = z.prod(axis=1)
        losses[start : start + m] = 1.0 - growth @ w
    return losses


METHODS = {"naive": block_naive, "vectorized": block_vectorized}


def chunk_blocks(p: Params, chunk_index: int, n_chunks: int) -> list[int]:
    """n_chunks 個のワーカーに、連続したブロックを均等に割り当てる。"""
    return [int(b) for b in np.array_split(np.arange(p.n_blocks), n_chunks)[chunk_index]]


def run_chunk(p: Params, chunk_index: int = 0, n_chunks: int = 1, method: str = "vectorized") -> np.ndarray:
    fn = METHODS[method]
    blocks = chunk_blocks(p, chunk_index, n_chunks)
    if not blocks:
        return np.empty(0)
    return np.concatenate([fn(p, b) for b in blocks])


def summarize(losses: np.ndarray) -> dict:
    var95, var99 = np.quantile(losses, [0.95, 0.99])
    return {
        "paths": int(losses.size),
        "mean_loss": float(losses.mean()),
        "var_95": float(var95),
        "var_99": float(var99),
        "cvar_99": float(losses[losses >= var99].mean()),
    }


def simulate(p: Params, method: str = "vectorized") -> dict:
    """1 プロセスで全部計算する（As-is の API サーバー内計算と同じ形）。"""
    return summarize(run_chunk(p, 0, 1, method))
