# -*- coding: utf-8 -*-
"""本地骑行库：所有来源的记录落地成本地 FIT + 一份索引。

为什么要有本地库：
  * 平台之间的拉取/推送经常要隔几天再补一次，缓存下来就不用反复打接口；
  * 去重、断点续传、事后核对都需要一个稳定的本地基准；
  * FIT 是能力上限最高的公共格式，落地成 FIT 可以再转成任何目标要的格式。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import config
from .fitlib import Activity, Point, build_fit
from .model import Ride
from .fitread import read_fit


def _index_path() -> Path:
    return config.lib_dir() / 'index.json'


def load_index() -> dict:
    p = _index_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def save_index(idx: dict) -> None:
    _index_path().write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding='utf-8')


def fit_path(fp: str) -> Path:
    return config.lib_dir() / f'{fp}.fit'


def _dt(s: str):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def similar(a: Ride, b: Ride) -> bool:
    """两次骑行是否是「同一次」：开始时间差 ≤3min 且 里程差 ≤max(200m,5%)。"""
    if not a.start or not b.start:
        return False
    if abs((a.start - b.start).total_seconds()) > 180:
        return False
    return abs(a.distance_m - b.distance_m) <= max(200.0, max(a.distance_m, b.distance_m) * 0.05)


def find_similar(ride: Ride) -> str | None:
    """找「同一次骑行」的已有记录。

    不同格式/平台导出的同一次骑行，开始时间和里程都会有细微差别（GPX 没有里程字段，
    只能用轨迹累加；平台之间可能差 1~3%），所以不能用哈希精确匹配，改用容差判定：
      开始时间差 ≤ 3 分钟 且 里程差 ≤ max(200m, 5%)
    """
    if not ride.start:
        return None
    for fp, meta in load_index().items():
        t = _dt(meta.get('start_utc') or '')
        if not t:
            continue
        d_old = float(meta.get('distance_km') or 0) * 1000
        probe = Ride(start=t, distance_m=d_old, points=[])
        if similar(ride, probe):
            return fp
    return None


def _channels(ride: Ride) -> int:
    return sum(1 for f in ('hr', 'cad', 'power') if any(getattr(p, f) for p in ride.points))


def _write_fit(ride: Ride, fp: str) -> None:
    if not ride.points:
        return
    pts = [Point(t=p.t, lat=p.lat, lon=p.lon, ele=p.ele, hr=p.hr, cad=p.cad,
                 power=p.power, speed=p.speed, dist=p.dist) for p in ride.points]
    blob = build_fit(pts, Activity(start=ride.start, distance_m=ride.distance_m,
                                   moving_s=ride.moving_s))
    fit_path(fp).write_bytes(blob)


def add(ride: Ride, log=None) -> tuple[str, bool]:
    """入库。返回 (指纹, 是否新增/升级)。

    * 已有完全相同的记录：不动，返回 False
    * 已有「同一次骑行」但格式更差（例如先导入的 GPX 没有心率/功率）：用更全的覆盖
    """
    ride.fix_totals()
    idx = load_index()
    fp = ride.fingerprint

    if fp in idx and fit_path(fp).exists():
        return fp, False

    same = find_similar(ride)
    if same and fit_path(same).exists():
        old_meta = idx.get(same, {})
        try:
            old = read_fit(fit_path(same))
        except Exception:
            old = None
        if old and _channels(ride) > _channels(old) and len(ride.points) >= len(old.points):
            _write_fit(ride, same)
            old_meta.update({k: v for k, v in ride.summary().items()
                             if k in ('points', 'has_hr', 'has_power', 'distance_km',
                                      'moving_min', 'elapsed_min')})
            old_meta['upgraded_at'] = datetime.now().isoformat(timespec='seconds')
            idx[same] = old_meta
            save_index(idx)
            if log:
                log(f'已用更完整的版本覆盖同一次骑行 {same}')
        return same, False

    _write_fit(ride, fp)
    meta = ride.summary()
    meta['imported_at'] = datetime.now().isoformat(timespec='seconds')
    meta['synced_to'] = []
    idx[fp] = meta
    save_index(idx)
    if log:
        log(f'新增 {fp}  {ride.describe()}')
    return fp, True


def mark_synced(fp: str, target: str) -> None:
    idx = load_index()
    if fp in idx:
        lst = set(idx[fp].get('synced_to') or [])
        lst.add(target)
        idx[fp]['synced_to'] = sorted(lst)
        save_index(idx)


def listing() -> list[dict]:
    idx = load_index()
    rows = list(idx.values())
    rows.sort(key=lambda r: r.get('start_utc') or '', reverse=True)
    return rows


def get(fp: str) -> Ride | None:
    p = fit_path(fp)
    if not p.exists():
        return None
    r = read_fit(p)
    meta = load_index().get(fp, {})
    r.title = meta.get('title') or r.title
    r.sid = meta.get('sid') or r.sid
    r.source = 'library'
    return r


def stats() -> dict:
    rows = listing()
    return {
        'count': len(rows),
        'km': round(sum(r.get('distance_km') or 0 for r in rows), 1),
        'first': rows[-1]['start'] if rows else '',
        'last': rows[0]['start'] if rows else '',
    }
