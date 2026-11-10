# -*- coding: utf-8 -*-
"""统一 HTTP 入口：先把 CA 证书路径修好，再用 curl_cffi 的浏览器指纹发请求。

为什么必须用 curl_cffi：行者/iGPSPORT 的网关会按 TLS 指纹拦纯 requests。
cainfo.ensure() 必须在 import curl_cffi 之前调用（它把 cacert.pem 复制到纯 ASCII
路径并设 SSL_CERT_FILE），否则程序放在中文目录会报 curl error 77。
"""
from __future__ import annotations

from . import cainfo

CA_FILE = cainfo.ensure()

from curl_cffi import requests  # noqa: E402


def get(url: str, **kw):
    kw.setdefault('impersonate', 'chrome')
    kw.setdefault('timeout', 60)
    return requests.get(url, **kw)


def post(url: str, **kw):
    kw.setdefault('impersonate', 'chrome')
    kw.setdefault('timeout', 60)
    return requests.post(url, **kw)


def put_raw(url: str, body: bytes) -> None:
    """OSS 预签名上传：Content-Type 必须存在且为空，否则签名不匹配。

    curl_cffi 会把空字符串覆盖成 application/octet-stream，只能用 http.client 原样发。
    """
    import http.client
    import urllib.parse

    u = urllib.parse.urlparse(url)
    cls = http.client.HTTPSConnection if u.scheme == 'https' else http.client.HTTPConnection
    conn = cls(u.netloc, timeout=180)
    try:
        target = u.path + ('?' + u.query if u.query else '')
        conn.request('PUT', target, body=body, headers={'Content-Type': ''})
        resp = conn.getresponse()
        data = resp.read()
        if resp.status >= 300:
            raise RuntimeError(f'OSS PUT HTTP {resp.status}: {data[:200].decode(errors="replace")}')
    finally:
        conn.close()


def form(url: str, fields: dict[str, str], **kw):
    """multipart/form-data 或 urlencoded（Wahoo/Strava 上传要这种）。"""
    kw.setdefault('impersonate', 'chrome')
    kw.setdefault('timeout', 120)
    return requests.post(url, data=fields, **kw)


def multipart(url: str, parts: list[dict], **kw):
    """真 multipart/form-data 上传。

    curl_cffi 0.17 起不再支持 requests 风格的 `files=`，必须用 CurlMime：
      parts = [{'name': 'file', 'filename': 'a.fit', 'data': blob,
                'content_type': 'application/octet-stream'}]
    文本字段只要 name + data，不要给 filename（给了会被当成文件）。
    """
    from curl_cffi import CurlMime

    kw.setdefault('impersonate', 'chrome')
    kw.setdefault('timeout', 180)
    mime = CurlMime.from_list(parts)
    try:
        return requests.post(url, multipart=mime, **kw)
    finally:
        mime.close()
