# -*- coding: utf-8 -*-
"""Wahoo 适配器：官方 Cloud API，可推 FIT（workout_file_uploads）。

Wahoo 的开放程度比 Garmin 好：Cloud API 支持 self-serve 申请，scope 里有
workouts_write，接口 /v1/workout_file_uploads 直接吃 base64 的 FIT。

准备：https://developers.wahooligan.com/ 建应用（需审核，通常几天），
拿到 client_id/secret 并授权得到 refresh_token，填进配置。

拉取这一侧现行文档没给出「下载原始活动文件」的公开接口，所以本适配器只做推送，
不假装支持拉取——这也是能力矩阵里 Wahoo 标 "push only" 的原因。
"""
from __future__ import annotations

import base64
import time

from .. import config, net
from ..fitlib import Activity, Point, build_fit
from ..model import Ride
from .base import Adapter

AUTH = 'https://api.wahooligan.com/oauth/token'
API = 'https://api.wahooligan.com/v1'


class Wahoo(Adapter):
    key = 'wahoo'
    name = 'Wahoo（ELEMNT/BOLT/ROAM）'
    caps = ('push',)
    needs = ('wahoo_client_id', 'wahoo_client_secret', 'wahoo_refresh_token')
    note = '只能推送（官方未公开活动原始文件下载接口）。需要 Wahoo 开发者应用，审核通过后可用。'

    def ready(self):
        c = config.load()
        if not (c.get('wahoo_refresh_token') or '').strip():
            return False, '缺少 refresh_token'
        if not (c.get('wahoo_client_id') and c.get('wahoo_client_secret')):
            return False, '缺少 client_id / client_secret'
        return True, ''

    def token(self) -> str:
        c = config.load()
        if c.get('wahoo_access_token') and int(c.get('wahoo_expires_at') or 0) > time.time() + 120:
            return c['wahoo_access_token']
        r = net.post(AUTH, data={
            'client_id': c['wahoo_client_id'], 'client_secret': c['wahoo_client_secret'],
            'grant_type': 'refresh_token', 'refresh_token': c['wahoo_refresh_token'],
        })
        if r.status_code != 200:
            raise RuntimeError(f'Wahoo 刷新令牌失败 HTTP {r.status_code}: {r.text[:120]}')
        d = r.json()
        config.save(wahoo_access_token=d.get('access_token', ''),
                    wahoo_refresh_token=d.get('refresh_token', c['wahoo_refresh_token']),
                    wahoo_expires_at=int(d.get('expires_at') or time.time() + int(d.get('expires_in') or 0)))
        return d['access_token']

    def push(self, ride: Ride, log=None) -> str:
        if not ride.points:
            raise RuntimeError('没有轨迹点')
        pts = [Point(t=p.t, lat=p.lat, lon=p.lon, ele=p.ele, hr=p.hr, cad=p.cad,
                     power=p.power, speed=p.speed, dist=p.dist) for p in ride.points]
        blob = build_fit(pts, Activity(start=ride.start, distance_m=ride.distance_m,
                                       moving_s=ride.moving_s))
        b64 = base64.b64encode(blob).decode()
        r = net.post(f'{API}/workout_file_uploads',
                     data={'workout_file': f'data:application/octet-stream;base64,{b64}',
                           'filename': f'{ride.fingerprint}.fit'},
                     headers={'Authorization': f'Bearer {self.token()}'})
        if r.status_code >= 300:
            raise RuntimeError(f'Wahoo 上传失败 HTTP {r.status_code}: {r.text[:160]}')
        job = r.json() if r.text else {}
        tok = job.get('id') or job.get('token')
        if not tok:
            return 'wahoo:queued'
        for _ in range(20):
            time.sleep(2)
            q = net.get(f'{API}/workout_file_uploads/{tok}',
                        headers={'Authorization': f'Bearer {self.token()}'}).json()
            if str(q.get('status')).lower() in ('complete', 'completed', 'success'):
                return f'wahoo:{q.get("workout_id") or tok}'
            if str(q.get('status')).lower() in ('failed', 'error'):
                raise RuntimeError(f'Wahoo 解析失败: {q}')
        return f'wahoo:queued-{tok}'
