# -*- coding: utf-8 -*-
"""行者（imxingzhe）适配器：只能拉取。

接口是网页端自用的 v1 API（Bearer token），已用真实数据验证全量导出：
  列表 GET /api/v1/pgworkout/?offset=N
  轨迹 GET /api/v1/pgworkout/{id}/stream/    （timestamp 秒、location [经度,纬度]）
  详情 GET /api/v1/pgworkout/{id}/            （distance 米、duration 秒）
  功率 GET /api/v1/pgworkout/{id}/points/     （time 毫秒）
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from .. import config, net
from ..model import Ride, RidePoint
from .base import Adapter

BASE = 'https://www.imxingzhe.com'


def _hdr(token: str) -> dict:
    # X-Requested-With 是验证版使用的同一套头，别删
    return {'Authorization': f'Bearer {token}', 'Referer': BASE + '/',
            'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}


class Xingzhe(Adapter):
    key = 'xingzhe'
    name = '行者'
    caps = ('pull',)
    needs = ('xz_token',)
    note = '只读：行者没有开放上传接口，只能把记录取出来。用手机行者 App 扫码登录，token 约 7 天过期。'

    def _token(self) -> str:
        return (config.load().get('xz_token') or '').strip()

    def ready(self):
        if not self._token():
            return False, '缺少行者 token'
        return True, ''

    def _get(self, path: str, params: dict | None = None):
        r = net.get(BASE + path, params=params, headers=_hdr(self._token()))
        if r.status_code == 401:
            raise RuntimeError('行者 token 失效，请重新登录')
        if r.status_code != 200:
            raise RuntimeError(f'GET {path} -> HTTP {r.status_code}: {r.text[:120]}')
        return r.json()

    def list_all(self, limit: int = 0, log=None) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            d = self._get('/api/v1/pgworkout/', {'offset': offset})
            batch = (d.get('data') or {}).get('data') or []
            if not batch:
                break
            out.extend(batch)
            offset += len(batch)
            if limit and len(out) >= limit:
                out = out[:limit]
                if log:
                    log(f'行者已列出 {len(out)} 条（已到设定的上限）')
                return out
            if log:
                log(f'行者已列出 {len(out)} 条')
            if len(batch) < 20:
                break
            time.sleep(0.25)
        return out

    def pull(self, limit: int = 0, log=None) -> list[Ride]:
        recs = self.list_all(limit, log)
        rides: list[Ride] = []
        for i, rec in enumerate(recs, 1):
            wid = rec.get('id')
            try:
                ride = self._one(rec, log)
                if ride and ride.points:
                    rides.append(ride)
                    if log:
                        log(f'[{i}/{len(recs)}] {ride.describe()}')
            except Exception as exc:
                if log:
                    log(f'[{i}/{len(recs)}] 跳过 {wid}: {exc}')
            time.sleep(0.35)
        return rides

    def _one(self, rec: dict, log=None) -> Ride | None:
        wid = rec['id']
        stream = (self._get(f'/api/v1/pgworkout/{wid}/stream/').get('data') or {})
        detail = (self._get(f'/api/v1/pgworkout/{wid}/').get('data') or {}).get('workout') or {}
        loc = stream.get('location') or []
        ts = stream.get('timestamp') or []
        if not loc or not ts:
            raise RuntimeError('无轨迹（可能是室内记录）')

        # 功率单独在 points 里，按秒对齐
        power: dict[int, float] = {}
        try:
            pts = (self._get(f'/api/v1/pgworkout/{wid}/points/') or {}).get('points') or []
            for p in pts:
                v = p.get('power')
                if v:
                    t = float(p.get('time') or 0)
                    power[int(t / 1000 if t > 1e11 else t)] = v
        except Exception:
            pass

        li, gi = self._orient(loc)
        cols = {k: stream.get(k) or [] for k in
                ('altitude', 'heartrate', 'speed', 'cadence', 'distance')}

        def at(k, i):
            arr = cols[k]
            return arr[i] if i < len(arr) else None

        points: list[RidePoint] = []
        for i in range(min(len(loc), len(ts))):
            t = float(ts[i])
            sec = int(t / 1000 if t > 1e11 else t)
            hr, cad, dist = at('heartrate', i), at('cadence', i), at('distance', i)
            points.append(RidePoint(
                t=datetime.fromtimestamp(sec, tz=timezone.utc),
                lat=loc[i][li], lon=loc[i][gi],
                ele=at('altitude', i),
                hr=int(hr) if hr else None,
                cad=int(cad) if cad else None,
                power=int(power[sec]) if sec in power else None,
                speed=at('speed', i),
                dist=float(dist) if dist is not None else None,
            ))
        # 标题只取行者自己给的，没有就留空 —— 与验证版一致。
        # 别自己编「行者 12345」：佳明推送会把标题写成活动名，会污染目标平台。
        ride = Ride(source=self.key, sid=str(wid), title=rec.get('title') or '',
                    points=points)
        ride.distance_m = float(detail.get('distance') or rec.get('distance') or 0)
        ride.moving_s = float(detail.get('duration') or 0)
        ride.start = points[0].t
        return ride.fix_totals()

    @staticmethod
    def _orient(loc: list) -> tuple[int, int]:
        """location 是 [a, b]，用中国经纬度范围判断哪一维是纬度。"""
        for a, b in loc[:50]:
            if 3 <= a <= 54 and 73 <= b <= 136:
                return 0, 1
            if 3 <= b <= 54 and 73 <= a <= 136:
                return 1, 0
        return 0, 1
