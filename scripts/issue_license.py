#!/usr/bin/env python3
"""签发 audio-hub 授权码（作者工具，私钥不入仓库）。

用法：
    # 首次：生成密钥对（私钥务必离线保管；公钥填进部署环境变量）
    python scripts/issue_license.py genkey --out-dir ./license-keys

    # 签发（默认永不过期；--days 指定有效期）
    python scripts/issue_license.py issue --user <github用户名> \
        [--days 365] [--private-key ./license-keys/license_ed25519.pem]

对应验证公钥放到部署环境：AUDIO_HUB_LICENSE_PUBLIC_KEY（32 字节 raw 的 base64），
genkey 结束时会直接打印这行环境变量值。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
from pathlib import Path

REPO = "OneFlatWhite/audio-hub"


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def cmd_genkey(args: argparse.Namespace) -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    priv_path = out / "license_ed25519.pem"
    pub_path = out / "license_ed25519.pub.b64"
    if priv_path.exists() and not args.force:
        print(f"私钥已存在：{priv_path}（--force 覆盖）")
        return 1
    key = Ed25519PrivateKey.generate()
    priv_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    pub_raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    pub_path.write_text(_b64u(pub_raw))
    print(f"私钥（离线保管，勿入仓库）：{priv_path}")
    print(f"公钥文件：{pub_path}")
    print("部署环境变量：")
    print(f'  AUDIO_HUB_LICENSE_PUBLIC_KEY={_b64u(pub_raw)}')
    return 0


def cmd_issue(args: argparse.Namespace) -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv_path = Path(args.private_key)
    if not priv_path.exists():
        print(f"私钥不存在：{priv_path}（先运行 genkey）", file=sys.stderr)
        return 1
    key = serialization.load_pem_private_key(priv_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        print(f"{priv_path} 不是 Ed25519 私钥", file=sys.stderr)
        return 1
    payload = {
        "v": 1,
        "user": args.user,
        "repo": REPO,
        "jti": args.jti or hashlib.sha256(f"{args.user}:{time.time()}".encode()).hexdigest()[:12],
        "issued_at": int(time.time()),
        "exp": int(time.time() + args.days * 86400) if args.days else None,
    }
    payload_raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    sig = key.sign(payload_raw)
    license_key = f"AH1.{_b64u(payload_raw)}.{_b64u(sig)}"
    print(license_key)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(required=True)
    g = sub.add_parser("genkey", help="生成 Ed25519 密钥对")
    g.add_argument("--out-dir", default="./license-keys")
    g.add_argument("--force", action="store_true")
    g.set_defaults(func=cmd_genkey)
    i = sub.add_parser("issue", help="签发授权码")
    i.add_argument("--user", required=True, help="GitHub 用户名（写进授权）")
    i.add_argument("--days", type=int, default=0, help="有效期天数；0 = 永久")
    i.add_argument("--jti", default="", help="授权唯一 ID（配合发放端吊销核对；默认自动生成）")
    i.add_argument("--private-key", default="./license-keys/license_ed25519.pem")
    i.set_defaults(func=cmd_issue)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
