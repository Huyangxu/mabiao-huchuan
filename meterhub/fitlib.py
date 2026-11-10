# -*- coding: utf-8 -*-
"""最小 FIT 编解码库（纯标准库）。

只覆盖骑行记录需要的消息：file_id / record / lap / session / activity。
几个必须记住的坑（踩过）：
  * 基础类型号必须带 0x80 高位：uint16=0x84 sint32=0x85 uint32=0x86 uint32z=0x8C。
    写成 0x04/0x05/0x06 是非法类型号，标准解析器会读错宽度，时间/坐标/里程全错位。
  * 单位按 FIT Profile 的 scale：elapsed/timer 毫秒(×1000)、distance 厘米(×100)。
  * CRC 表末尾的 1 不能省，否则文件 CRC 校验失败，服务端会静默丢弃整个文件。
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

FIT_EPOCH = 631065600  # 1989-12-31 00:00:00 UTC
CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)
SEMICIRCLE = 2 ** 31 / 180.0


def fit_crc(data: bytes, crc: int = 0) -> int:
    for byte in data:
        tmp = CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc ^= tmp ^ CRC_TABLE[byte & 0xF]
        tmp = CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc ^= tmp ^ CRC_TABLE[(byte >> 4) & 0xF]
    return crc & 0xFFFF


def fit_time(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) - FIT_EPOCH


def from_fit_time(value: int) -> datetime:
    return datetime.fromtimestamp(value + FIT_EPOCH, tz=timezone.utc)


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371008.8 * 2 * math.asin(min(1.0, math.sqrt(h)))


@dataclass
class Point:
    t: datetime | None
    lat: float
    lon: float
    ele: float | None = None
    hr: int | None = None
    cad: int | None = None
    power: int | None = None
    speed: float | None = None
    dist: float | None = None  # 累计里程（米）


@dataclass
class Activity:
    start: datetime | None = None
    distance_m: float = 0.0
    moving_s: float = 0.0  # 骑行时长（去停顿），写 session.total_timer_time


MESSAGES = {
    'file_id': (0, 0, [(0, 1, 0x00), (1, 2, 0x84), (2, 2, 0x84), (3, 4, 0x8C), (4, 4, 0x86)]),
    'record': (1, 20, [(253, 4, 0x86), (0, 4, 0x85), (1, 4, 0x85), (5, 4, 0x86),
                       (78, 4, 0x86), (73, 4, 0x86), (3, 1, 0x02), (4, 1, 0x02), (7, 2, 0x84)]),
    'session': (2, 18, [(253, 4, 0x86), (2, 4, 0x86), (5, 1, 0x00), (7, 4, 0x86),
                        (8, 4, 0x86), (9, 4, 0x86)]),
    'lap': (4, 19, [(253, 4, 0x86), (254, 2, 0x84), (2, 4, 0x86),
                    (7, 4, 0x86), (8, 4, 0x86), (9, 4, 0x86),
                    (3, 4, 0x85), (4, 4, 0x85), (5, 4, 0x85), (6, 4, 0x85)]),
    'activity': (3, 34, [(253, 4, 0x86), (0, 4, 0x86), (1, 2, 0x84),
                         (2, 1, 0x00), (3, 1, 0x00), (4, 1, 0x00), (5, 4, 0x86)]),
}


def _invalid(base: int) -> int:
    t = base & 0x1F
    if t in (0x00, 0x02, 0x0A, 0x0D):
        return 0xFF
    if t == 0x01:
        return 0x7F
    if t == 0x03:
        return 0x7FFF
    if t in (0x04, 0x0B):
        return 0xFFFF
    if t == 0x05:
        return 0x7FFFFFFF
    return 0xFFFFFFFF


def _pack(value, size: int, base: int) -> bytes:
    t = base & 0x1F
    v = _invalid(base) if value is None else int(value)
    if t == 0x01:
        return struct.pack('<b', v)
    if t == 0x03:
        return struct.pack('<h', v)
    if t == 0x05:
        return struct.pack('<i', v)
    if size == 1:
        return struct.pack('<B', v & 0xFF)
    if size == 2:
        return struct.pack('<H', v & 0xFFFF)
    return struct.pack('<I', v & 0xFFFFFFFF)


def build_fit(points: list[Point], act: Activity, manufacturer: int = 1, product: int = 1234) -> bytes:
    chunks: list[bytes] = []
    written: set[str] = set()

    def emit(kind: str, values: dict[int, object]) -> None:
        lmn, gmn, fields = MESSAGES[kind]
        if kind not in written:
            definition = struct.pack('<BBBHB', 0x40 | lmn, 0, 0, gmn, len(fields))
            definition += b''.join(struct.pack('<BBB', f, s, b) for f, s, b in fields)
            chunks.append(definition)
            written.add(kind)
        row = struct.pack('<B', lmn)
        row += b''.join(_pack(values.get(f), s, b) for f, s, b in fields)
        chunks.append(row)

    times = [p.t for p in points if p.t]
    start = min(times) if times else act.start
    finish = max(times) if times else start
    start_ts = fit_time(start)
    end_ts = fit_time(finish) or start_ts
    elapsed_ms = int((finish - start).total_seconds() * 1000) if start and finish else 0
    timer_ms = int(act.moving_s * 1000) if act.moving_s and act.moving_s > 0 else elapsed_ms
    dist_cm = int(act.distance_m * 100)

    cum: list[float] = []
    run = 0.0
    prev = None
    for p in points:
        if p.dist is not None:
            run = p.dist
        elif prev is not None:
            run += haversine((prev.lat, prev.lon), (p.lat, p.lon))
        cum.append(run)
        prev = p

    emit('file_id', {0: 4, 1: manufacturer, 2: product, 3: 1, 4: start_ts})
    for p, d_m in zip(points, cum):
        emit('record', {
            253: fit_time(p.t),
            0: int(p.lat * SEMICIRCLE),
            1: int(p.lon * SEMICIRCLE),
            5: int(d_m * 100),
            78: int((p.ele + 500) * 5) if p.ele is not None else None,
            73: int(p.speed * 1000) if p.speed is not None and p.speed >= 0 else None,
            3: p.hr,
            4: p.cad,
            7: p.power,
        })
    first, last = (points[0], points[-1]) if points else (None, None)
    s_lat, s_lon = (int(first.lat * SEMICIRCLE), int(first.lon * SEMICIRCLE)) if first else (None, None)
    e_lat, e_lon = (int(last.lat * SEMICIRCLE), int(last.lon * SEMICIRCLE)) if last else (None, None)
    emit('lap', {253: end_ts, 254: 0, 2: start_ts, 7: elapsed_ms, 8: timer_ms, 9: dist_cm,
                 3: s_lat, 4: s_lon, 5: e_lat, 6: e_lon})
    emit('session', {253: end_ts, 2: start_ts, 5: 2, 7: elapsed_ms, 8: timer_ms, 9: dist_cm})
    # activity.local_timestamp = UTC + 时区偏移（中国 +8h），帮助平台按当地时间归档日期
    local_ts = (end_ts + 8 * 3600) if end_ts is not None else None
    emit('activity', {253: end_ts, 0: elapsed_ms, 1: 1, 2: 0, 3: 26, 4: 1, 5: local_ts})

    body = b''.join(chunks)
    header = struct.pack('<BBHI4s', 14, 0x10, 2110, len(body), b'.FIT')
    header += struct.pack('<H', fit_crc(header))
    return header + body + struct.pack('<H', fit_crc(header + body))


def _decode(raw: bytes, base: int, little: bool) -> int:
    t = base & 0x1F
    o = '<' if little else '>'
    if t == 0x01:
        return struct.unpack(o + 'b', raw[:1])[0]
    if t == 0x03:
        return struct.unpack(o + 'h', raw[:2])[0]
    if t == 0x05:
        return struct.unpack(o + 'i', raw[:4])[0]
    if t in (0x04, 0x0B):
        return struct.unpack(o + 'H', raw[:2])[0]
    if t in (0x06, 0x0C):
        return struct.unpack(o + 'I', raw[:4])[0]
    return int.from_bytes(raw, 'little' if little else 'big')


def inspect_fit(path: Path) -> dict:
    """读回自己写的/别人的 FIT，返回点数/开始时间/里程/是否含心率等，用于上传前自检。"""
    data = Path(path).read_bytes()
    out = {'points': 0, 'start': None, 'distance_m': 0.0, 'elapsed_s': 0.0,
           'has_hr': False, 'has_cad': False, 'has_power': False, 'issues': []}
    hsize = data[0] if data else 0
    if hsize not in (12, 14) or len(data) < hsize + 2 or data[8:12] != b'.FIT':
        out['issues'].append('不是有效的 FIT 文件')
        return out
    size = struct.unpack_from('<I', data, 4)[0]
    end = min(len(data) - 2, hsize + size)
    body = data[hsize:end]
    if end + 2 <= len(data) and struct.unpack_from('<H', data, end)[0] != fit_crc(data[:end]):
        out['issues'].append('文件 CRC 不匹配')
    defs: dict[int, dict] = {}
    pos = 0
    positions: list[tuple[float, float]] = []
    while pos < len(body):
        rh = body[pos]
        pos += 1
        lmn = (rh >> 5) & 0x03 if rh & 0x80 else rh & 0x0F
        if rh & 0x40:
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
            if rh & 0x08 and pos < len(body):
                ndf = body[pos]
                pos += 1 + 3 * ndf
            defs[lmn] = {'gmn': gmn, 'fields': fields, 'little': little}
            continue
        d = defs.get(lmn)
        if not d:
            break
        vals: dict[int, int] = {}
        for fdn, fsize, base in d['fields']:
            if pos + fsize > len(body):
                pos = len(body)
                break
            if not (rh & 0x80 and fdn == 253):
                vals[fdn] = _decode(body[pos:pos + fsize], base, d['little'])
            pos += fsize
        if d['gmn'] == 20:
            out['points'] += 1
            if 253 in vals and out['start'] is None:
                out['start'] = from_fit_time(vals[253])
            if 0 in vals and 1 in vals:
                positions.append((vals[0] / SEMICIRCLE, vals[1] / SEMICIRCLE))
            if 3 in vals and vals[3] != 0xFF:
                out['has_hr'] = True
            if 4 in vals and vals[4] != 0xFF:
                out['has_cad'] = True
            if 7 in vals and vals[7] != 0xFFFF:
                out['has_power'] = True
        elif d['gmn'] == 18 and 9 in vals and vals[9] != 0xFFFFFFFF:
            out['distance_m'] = vals[9] / 100.0
            out['elapsed_s'] = vals.get(7, 0) / 1000.0
    if out['distance_m'] == 0 and len(positions) > 1:
        out['distance_m'] = sum(haversine(positions[i], positions[i + 1])
                                for i in range(len(positions) - 1))
    if out['points'] == 0:
        out['issues'].append('没有轨迹点')
    return out
