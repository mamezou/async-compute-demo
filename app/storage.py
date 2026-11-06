"""ジョブの入力・途中結果・結果の置き場所。ローカルではディレクトリ、AWS では S3。"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import numpy as np


class LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def put_bytes(self, key: str, data: bytes) -> None:
        tmp = self._path(key + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(self._path(key))  # 書きかけを読まれないように rename で置く

    def get_bytes(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def exists(self, key: str) -> bool:
        return (self.root / key).exists()


class S3Storage:
    def __init__(self, bucket: str):
        import boto3

        self.bucket = bucket
        self.s3 = boto3.client("s3")

    def put_bytes(self, key: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get_bytes(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise


def put_json(storage, key: str, obj: dict) -> None:
    storage.put_bytes(key, json.dumps(obj, ensure_ascii=False).encode())


def get_json(storage, key: str) -> dict:
    return json.loads(storage.get_bytes(key))


def put_array(storage, key: str, arr: np.ndarray) -> None:
    buf = io.BytesIO()
    np.save(buf, arr)
    storage.put_bytes(key, buf.getvalue())


def get_array(storage, key: str) -> np.ndarray:
    return np.load(io.BytesIO(storage.get_bytes(key)))


def from_env():
    bucket = os.environ.get("BUCKET")
    if bucket:
        return S3Storage(bucket)
    return LocalStorage(os.environ.get("DATA_DIR", ".data"))


# キーの決まり
def input_key(job_id: str) -> str:
    return f"jobs/{job_id}/input.json"


def part_key(job_id: str, index: int) -> str:
    return f"jobs/{job_id}/parts/{index:04d}.npy"


def result_key(job_id: str) -> str:
    return f"jobs/{job_id}/result.json"


def meta_key(job_id: str) -> str:
    return f"jobs/{job_id}/meta.json"
