# -*- coding: utf-8 -*-
"""CA 证书定位。

curl_cffi 内置的 libcurl 读证书文件时用的是窄字符路径，只要程序落在中文目录
（例如含中文字符的目录名）就会报 curl error 77，所有 HTTPS 请求失败。
这里在 import curl_cffi 之前把证书复制到纯 ASCII 路径，并用 SSL_CERT_FILE 指过去。

必须在 `from curl_cffi import requests` 之前 import 本模块。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def _is_ascii(p: Path) -> bool:
    try:
        str(p).encode("ascii")
        return True
    except (UnicodeEncodeError, UnicodeDecodeError):
        return False


def _candidates():
    pd = os.environ.get("ProgramData") or r"C:\ProgramData"
    yield Path(pd) / "xz2igp"
    tmp = os.environ.get("TEMP") or os.environ.get("TMP")
    if tmp:
        yield Path(tmp) / "xz2igp"
    for letter in ("C:", "D:", "E:", "F:"):
        yield Path(letter + os.sep + "xz2igp-ca")


def ensure() -> str:
    """返回一个可用的、路径为纯 ASCII 的 CA 文件路径。"""
    cur = os.environ.get("SSL_CERT_FILE")
    if cur and Path(cur).is_file() and _is_ascii(Path(cur)):
        return cur

    try:
        import certifi

        src = Path(certifi.where())
    except Exception:
        return cur or ""

    if not src.is_file():
        return str(src)
    if _is_ascii(src):
        os.environ.setdefault("SSL_CERT_FILE", str(src))
        os.environ.setdefault("CURL_CA_BUNDLE", str(src))
        return str(src)

    for d in _candidates():
        if not _is_ascii(d):
            continue
        try:
            d.mkdir(parents=True, exist_ok=True)
            dst = d / "cacert.pem"
            if not dst.is_file() or dst.stat().st_size != src.stat().st_size:
                shutil.copyfile(src, dst)
            os.environ["SSL_CERT_FILE"] = str(dst)
            os.environ["CURL_CA_BUNDLE"] = str(dst)
            os.environ.pop("REQUESTS_CA_BUNDLE", None)
            return str(dst)
        except OSError:
            continue

    # 实在找不到可写位置，只能退回原路径（中文路径下大概率失败，但至少能跑 ASCII 场景）
    return str(src)


CA_FILE = ensure()
