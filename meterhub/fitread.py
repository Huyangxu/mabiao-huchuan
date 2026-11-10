# -*- coding: utf-8 -*-
"""FIT 读取：把任意来源的 FIT（Garmin/Wahoo/Bryton/迈金/iGPSPORT/行者…）解成 Ride。

关键点（真机上踩过）：
  * 数据消息头可能带压缩时间戳（bit7=1）：此时该条消息不含 253 字段，
    时间要用上一条的时间戳 + 5bit 偏移还原。Garmin 大量使用这种压缩形式，
    不处理就会有一半轨迹没有时间。
  * 字段宽度必须按「基础类型号 & 0x1F」判定，不能只看 size；
    size 大于基础宽度时是数组。
  * 无效值（invalid）要当成缺失，而不是 0，否则会出现海拔 -500m、心率 255。
"""
from __future__ import annotations

import struct
from datetime import datetime
from pathlib import Path

from .fitlib import SEMICIRCLE, from_fit_time
from .model import Ride, RidePoint

# 基础类型号 -> (struct 字符, 宽度, invalid)
BASE = {
    0x01: ('b', 1, 0x7F), 0x02: ('B', 1, 0xFF),
    0x03: ('h', 2, 0x7FFF), 0x04: ('H', 2, 0xFFFF),
    0x05: ('i', 4, 0x7FFFFFFF), 0x06: ('I', 4, 0xFFFFFFFF),
    0x08: ('f', 4, None), 0x09: ('d', 8, None),
    0x0A: ('B', 1, 0xFF), 0x0B: ('H', 2, 0xFFFF), 0x0C: ('I', 4, 0xFFFFFFFF),
    0x0D: ('B', 1, 0xFF),
}

G_RECORD, G_SESSION, G_LAP = 20, 18, 19


def _decode_field(raw: bytes, base: int, little: bool) -> object:
    t = base & 0x1F
    if t == 0x07:  # string
        return raw.split(b'\x00')[0].decode('utf-8', 'ignore')
    if t == 0x0D:
        return raw[0] if raw else 0
    spec = BASE.get(t)
    if not spec:
        return int.from_bytes(raw, 'little' if little else 'big')
    ch, width, invalid = spec
    endian = '<' if little else '>'
    n = len(raw) // width
    if n <= 1:
        v = struct.unpack(endian + ch, raw[:width])[0]
        return None if (invalid is not None and v == invalid) else v
    vals = []
    for i in range(n):
        v = struct.unpack(endian + ch, raw[i * width:(i + 1) * width])[0]
        vals.append(None if (invalid is not None and v == invalid) else v)
    return vals


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def read_fit(path: str | Path) -> Ride:
    """从文件读 FIT。文件名当默认 sid/title（界面里另有日期列，不改显示）。"""
    p = Path(path)
    return read_fit_bytes(p.read_bytes(), sid=p.stem, title=p.stem)


def read_fit_bytes(data: bytes, sid: str = '', title: str = '') -> Ride:
    """从内存字节读 FIT。

    iGPSPORT 的 getDownloadUrl 返回的是 OSS 直链（不带扩展名），
    顽鹿也是直接给字节流，两边都不想为了解析先落个临时文件。
    """
    hsize = data[0] if data else 0
    if hsize not in (12, 14) or len(data) < hsize + 2 or data[8:12] != b'.FIT':
        raise ValueError('不是有效的 FIT 文件')
    size = struct.unpack_from('<I', data, 4)[0]
    body = data[hsize:min(len(data) - 2, hsize + size)]

    defs: dict[int, dict] = {}
    pts: list[RidePoint] = []
    sess: dict[int, object] = {}
    last_ts: int | None = None
    pos = 0
    while pos < len(body):
        rh = body[pos]
        pos += 1
        if rh & 0x40:  # definition
            if pos + 5 > len(body):
                break
            pos += 1
            little = body[pos] == 0
            pos += 1
            gmn = struct.unpack_from('<H' if little else '>H', body, pos)[0]
            pos += 2
            nf = body[pos]
            pos += 1
            fields = []
            for _ in range(nf):
                if pos + 3 > len(body):
                    break
                fields.append((body[pos], body[pos + 1], body[pos + 2]))
                pos += 3
            if rh & 0x20 and pos < len(body):  # developer fields
                ndf = body[pos]
                pos += 1 + 3 * ndf
            defs[rh & 0x0F if not (rh & 0x80) else (rh >> 5) & 0x03] = {
                'gmn': gmn, 'fields': fields, 'little': little}
            continue

        compressed = bool(rh & 0x80)
        lmt = (rh >> 5) & 0x03 if compressed else rh & 0x0F
        d = defs.get(lmt)
        if not d:
            break
        vals: dict[int, object] = {}
        for fdn, fsize, fbase in d['fields']:
            if pos + fsize > len(body):
                pos = len(body)
                break
            vals[fdn] = _decode_field(body[pos:pos + fsize], fbase, d['little'])
            pos += fsize

        ts = None
        if compressed and last_ts is not None:
            off = rh & 0x1F
            ts = (last_ts & ~0x1F) + off
            if off < (last_ts & 0x1F):
                ts += 0x20
        elif 253 in vals and vals[253] is not None:
            ts = int(vals[253])
        if ts is not None:
            last_ts = ts

        gmn = d['gmn']
        if gmn == G_RECORD:
            lat, lon = _first(vals.get(0)), _first(vals.get(1))
            if ts is None or lat is None or lon is None:
                continue
            ele = _first(vals.get(78)) or _first(vals.get(2))
            spd = _first(vals.get(73)) or _first(vals.get(6))
            pts.append(RidePoint(
                t=from_fit_time(ts),
                lat=lat / SEMICIRCLE, lon=lon / SEMICIRCLE,
                ele=(ele / 5.0 - 500.0) if ele is not None else None,
                hr=_first(vals.get(3)), cad=_first(vals.get(4)),
                power=_first(vals.get(7)),
                speed=(spd / 1000.0) if spd else None,
                dist=(_first(vals.get(5)) / 100.0) if _first(vals.get(5)) is not None else None,
            ))
        elif gmn == G_SESSION and not sess:
            sess = vals

    ride = Ride(source='fit', sid=sid, title=title, points=pts)
    if sess:
        st = _first(sess.get(2)) or _first(sess.get(253))
        if st:
            ride.start = from_fit_time(int(st))
        el, ti, di = _first(sess.get(7)), _first(sess.get(8)), _first(sess.get(9))
        ride.elapsed_s = (el or 0) / 1000.0
        ride.moving_s = (ti or 0) / 1000.0
        ride.distance_m = (di or 0) / 100.0
    ride.fix_totals()
    if ride.title.startswith(('202', '19', '20')) and len(ride.title) >= 15:
        ride.title = ride.title  # 保留文件名，界面里另有日期列
    return ride
