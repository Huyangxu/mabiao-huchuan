# -*- coding: utf-8 -*-
"""Strava 适配器：官方 API，可拉可推（需要自建 API 应用，免费）。

Strava 在骑行生态里几乎是唯一"两头都开"的平台，所以它常被当中转站：
  码表 → Strava → 其它平台。

准备（一次性，5 分钟）：
  1. https://www.strava.com/settings/api 创建应用，拿 Client ID / Client Secret
  2. 授权后拿 refresh_token（工具里给了一条授权链接的命令行助手）
  3. 把 client_id / client_secret / refresh_token 填进配置

接口：
  刷新令牌 POST https://www.strava.com/oauth/token
  列表     GET  /api/v3/athlete/activities?page&per_page
  流       GET  /api/v3/activities/{id}/streams?keys=...&key_by_type=true
  上传     POST /api/v3/uploads (multipart: file, data_type=fit)
  轮询     GET  /api/v3/uploads/{id}
"""
from __future__ import annotations

import base64
import time
from datetime import datetime, timedelta, timezone

from .. import config, net
from ..model import Ride, RidePoint
from .base import Adapter

AUTH = 'https://www.strava.com/oauth/token'
API = 'https://www.strava.com/api/v3'


class Strava(Adapter):
    key = 'strava'
    name = 'Strava'
    caps = ('pull', 'push')
    needs = ('strava_client_id', 'strava_client_secret', 'strava_refresh_token')
    note = '可拉可推。需要自己在 Strava 建一个 API 应用（免费），填入 Client ID/Secret/refresh_token。'

    # ---------------- 令牌 ----------------

    def ready(self):
        c = config.load()
        if not (c.get('strava_refresh_token') or '').strip():
            return False, '缺少 refresh_token'
        if not (c.get('strava_client_id') and c.get('strava_client_secret')):
            return False, '缺少 Client ID / Secret'
        return True, ''

    def token(self) -> str:
        c = config.load()
        if c.get('strava_access_token') and int(c.get('strava_expires_at') or 0) > time.time() + 120:
            return c['strava_access_token']
        r = net.post(AUTH, data={
            'client_id': c['strava_client_id'], 'client_secret': c['strava_client_secret'],
            'grant_type': 'refresh_token', 'refresh_token': c['strava_refresh_token'],
        })
        if r.status_code != 200:
            raise RuntimeError(f'Strava 刷新令牌失败 HTTP {r.status_code}: {r.text[:120]}')
        d = r.json()
        config.save(strava_access_token=d.get('access_token', ''),
                    strava_refresh_token=d.get('refresh_token', c['strava_refresh_token']),
                    strava_expires_at=int(d.get('expires_at') or 0))
        return d['access_token']

    def _h(self) -> dict:
        return {'Authorization': f'Bearer {self.token()}'}

    # ---------------- 拉取 ----------------

    def pull(self, limit: int = 0, log=None) -> list[Ride]:
        rides: list[Ride] = []
        page = 1
        while page <= 40:
            r = net.get(f'{API}/athlete/activities',
                        params={'page': page, 'per_page': 100}, headers=self._h())
            if r.status_code != 200:
                raise RuntimeError(f'列出活动失败 HTTP {r.status_code}: {r.text[:120]}')
            acts = r.json() or []
            if not acts:
                break
            for a in acts:
                if (a.get('type') or '').lower() not in ('ride', 'virtualride', 'ebikeride', 'gravelride', 'mountainbikeride'):
                    continue
                try:
                    rides.append(self._one(a, log))
                    if log:
                        log(f"{a.get('start_date_local','')[:16]}  {rides[-1].describe()}")
                except Exception as exc:
                    if log:
                        log(f"跳过 {a.get('id')}: {exc}")
                if limit and len(rides) >= limit:
                    return rides
                time.sleep(0.2)
            if len(acts) < 100:
                break
            page += 1
        return rides

    def _one(self, a: dict, log=None) -> Ride:
        sid = a['id']
        r = net.get(f'{API}/activities/{sid}/streams',
                    params={'keys': 'time,latlng,altitude,heartrate,cadence,watts,velocity_smooth,distance',
                            'key_by_type': 'true'}, headers=self._h())
        if r.status_code != 200:
            raise RuntimeError(f'取流失败 HTTP {r.status_code}')
        st = r.json() or {}
        t0 = st.get('time', {}).get('data') or []
        ll = st.get('latlng', {}).get('data') or []
        if not t0 or not ll:
            raise RuntimeError('没有 GPS 流')
        start = datetime.fromisoformat(a['start_date'].replace('Z', '+00:00'))

        def data(k):
            return (st.get(k) or {}).get('data') or []

        alt, hr, cad, wat, spd, dist = (data('altitude'), data('heartrate'), data('cadence'),
                                        data('watts'), data('velocity_smooth'), data('distance'))
        pts: list[RidePoint] = []
        for i, (sec, latlng) in enumerate(zip(t0, ll)):
            pts.append(RidePoint(
                t=start + timedelta(seconds=int(sec)),
                lat=latlng[0], lon=latlng[1],
                ele=alt[i] if i < len(alt) else None,
                hr=hr[i] if i < len(hr) else None,
                cad=cad[i] if i < len(cad) else None,
                power=wat[i] if i < len(wat) else None,
                speed=spd[i] if i < len(spd) else None,
                dist=dist[i] if i < len(dist) else None,
            ))
        ride = Ride(source=self.key, sid=str(sid), title=a.get('name') or f'Strava {sid}',
                    points=pts)
        ride.start = pts[0].t
        ride.distance_m = float(a.get('distance') or 0)
        ride.moving_s = float(a.get('moving_time') or 0)
        ride.elapsed_s = float(a.get('elapsed_time') or 0)
        ride.ascent_m = a.get('total_elevation_gain')
        return ride.fix_totals()

    # ---------------- 推送 ----------------

    def push(self, ride: Ride, log=None) -> str:
        if not ride.points:
            raise RuntimeError('没有轨迹点')
        from ..fitlib import Activity, Point, build_fit
        pts = [Point(t=p.t, lat=p.lat, lon=p.lon, ele=p.ele, hr=p.hr, cad=p.cad,
                     power=p.power, speed=p.speed, dist=p.dist) for p in ride.points]
        blob = build_fit(pts, Activity(start=ride.start, distance_m=ride.distance_m,
                                       moving_s=ride.moving_s))
        name = (ride.start.strftime('%Y%m%d_%H%M') if ride.start else 'ride') + '.fit'
        r = net.post(f'{API}/uploads',
                     data={'data_type': 'fit', 'name': ride.title or name,
                           'external_id': f'meterhub-{ride.fingerprint}'},
                     files={'file': (name, blob, 'application/octet-stream')},
                     headers=self._h())
        if r.status_code >= 300:
            raise RuntimeError(f'上传失败 HTTP {r.status_code}: {r.text[:160]}')
        up = r.json()
        uid = up.get('id')
        for _ in range(30):
            time.sleep(2)
            q = net.get(f'{API}/uploads/{uid}', headers=self._h()).json()
            if q.get('activity_id'):
                return f'strava:{q["activity_id"]}'
            if (q.get('error') or '').strip():
                raise RuntimeError(f'Strava 解析失败: {q["error"]}')
        return f'strava:upload-{uid}'
