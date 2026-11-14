#!/usr/bin/env python3
"""Upload a file to Cloudflare R2 bucket vdl-download with integrity verification.

Usage:
  R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... python3 r2_upload.py <local_file> <object_key> <content_type> [version]

Verifies: size + 3 random-range sha256 spot checks + metadata after upload.

传入 version 时会写成对象自定义元数据 x-amz-meta-version，下载页
（GET /download/meta）据此自动显示版本号，不必再手改页面文案。
"""
import hashlib
import os
import random
import sys
import time

# 直连 R2：macOS 上 urllib3 会从「系统网络配置」读到 WorkBuddy 注入的代理
# (127.0.0.1:60700)，仅清 HTTP_PROXY 等 env 无效，必须显式 NO_PROXY=* 才能
# 绕过，否则 64MB 分段上传在代理长连接上 SSL EOF（UNEXPECTED_EOF_WHILE_READING）。
for _v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_v, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

ENDPOINT = os.environ.get(
    "R2_ENDPOINT", "https://0ce96986e69a53d1413de7819b6ce5f7.r2.cloudflarestorage.com"
)
BUCKET = os.environ.get("R2_BUCKET", "vdl-download")
PUBLIC_BASE = os.environ.get("R2_PUBLIC_BASE", "https://download.hanyuxz.top")


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def range_sha256(path, start, length):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(8 * 1024 * 1024, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def main():
    if len(sys.argv) not in (4, 5):
        print(__doc__)
        sys.exit(2)
    local_path, key, content_type = sys.argv[1:4]
    version = (sys.argv[4].strip() if len(sys.argv) == 5 else "")
    if not os.path.isfile(local_path):
        print(f"FATAL: file not found: {local_path}")
        sys.exit(1)

    size = os.path.getsize(local_path)
    print(f"[1/4] local file : {local_path}")
    print(f"      size       : {size} ({human(size)})")
    print(f"      sha256     : {file_sha256(local_path)}")

    key_id = os.environ.get("R2_ACCESS_KEY_ID")
    secret = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not key_id or not secret:
        print("FATAL: R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY not set")
        sys.exit(1)

    client = boto3.Session(
        aws_access_key_id=key_id, aws_secret_access_key=secret
    ).client(
        "s3",
        endpoint_url=ENDPOINT,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
    )

    print(f"[2/4] uploading  : s3://{BUCKET}/{key}"
          + (f"  (version={version})" if version else ""))
    t0 = time.time()
    extra = {
        "ContentType": content_type,
        "CacheControl": "public, max-age=300",
        "ContentDisposition": f'attachment; filename="{os.path.basename(local_path)}"',
    }
    if version:
        extra["Metadata"] = {"version": version}
    client.upload_file(
        local_path,
        BUCKET,
        key,
        ExtraArgs=extra,
        Config=TransferConfig(
            multipart_threshold=64 * 1024 * 1024,
            multipart_chunksize=64 * 1024 * 1024,
            max_concurrency=4,
        ),
    )
    dt = time.time() - t0
    print(f"      uploaded   : {dt:.0f}s, avg {human(size / dt)}/s")

    print("[3/4] metadata    :")
    head = client.head_object(Bucket=BUCKET, Key=key)
    remote_size = head["ContentLength"]
    print(f"      size       : {remote_size}")
    print(f"      type       : {head.get('ContentType')}")
    print(f"      etag       : {head.get('ETag')}")
    if version:
        got = (head.get("Metadata") or {}).get("version", "")
        print(f"      meta ver   : {got or '(空)'}  {'OK' if got == version else 'MISMATCH'}")
        if got != version:
            print("FATAL: version metadata not persisted")
            sys.exit(1)
    if remote_size != size:
        print("FATAL: size mismatch between local and remote")
        sys.exit(1)

    print("[4/4] spot check  : 3 random 4MB ranges, sha256 local vs remote")
    random.seed(42)
    ok = True
    for i in range(3):
        if size <= 8 * 1024 * 1024:
            start, length = 0, size
        else:
            length = 4 * 1024 * 1024
            start = random.randint(0, size - length)
        body = client.get_object(Bucket=BUCKET, Key=key, Range=f"bytes={start}-{start + length - 1}")["Body"].read()
        remote_h = hashlib.sha256(body).hexdigest()
        local_h = range_sha256(local_path, start, length)
        match = remote_h == local_h
        ok = ok and match
        print(f"      range {start:>12}-{start + length - 1:<12} {'MATCH' if match else 'MISMATCH'}")
    if not ok:
        print("FATAL: spot-check hash mismatch")
        sys.exit(1)

    print(f"OK: {PUBLIC_BASE}/{key}")


if __name__ == "__main__":
    main()
