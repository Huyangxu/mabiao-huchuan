# -*- coding: utf-8 -*-
"""iGPSPORT（迹驰）适配器：可以推送 FIT，也能读到已有记录用于去重。

链路（网页端自用接口，已用真实数据全量验证）：
  1. GET  /service/sportg/third-party-server/oss/getSignedUrl?fileExtension=.fit
  2. PUT  signedUrl            （Content-Type 必须为空）
  3. POST /service/web-gateway/web-analyze/activity/uploadByOss {fileName, ossName}
  4. 服务端异步解析，日期/里程从 FIT 里读，所以 FIT 本身必须正确

限制：没有开放的"下载原始 FIT"接口，所以只能作为目标，不能当来源。
"""
from __future__ import annotations

import json
import time

from .. import config, net
from ..fitlib import Activity, Point, build_fit
from ..model import Ride
from .base import Adapter

REGIONS = {'cn': 'https://prod.zh.igpsport.com', 'intl': 'https://prod.en.igpsport.com'}


def _hdr(token: str = '') -> dict:
    h = {'Accept': 'application/json', 'Accept-Language': 'zh-CN',
         'TimeZone': 'Asia/Shanghai', 'Qiwu-App-Version': '7.07.08',
         'X-Platform': 'web', 'Content-Type': 'application/json'}
    if token:
        h['Authorization'] = f'Bearer {token}'
    return h


def _unwrap(resp) -> dict:
    try:
        j = resp.json()
    except ValueError:
        return {}
    if isinstance(j, dict) and isinstance(j.get('data'), (dict, list)):
        return j['data']
    return j if isinstance(j, dict) else {}


class IGPSPORT(Adapter):
    key = 'igpsport'
    name = 'iGPSPORT 迹驰'
    caps = ('pull', 'push')
    needs = ('igp_token',)
    note = ('可读可写：读 = 下载云端原始 FIT；写 = 上传 FIT（心率/踏频/功率全保留）。'
            'token 来自 App/网页端登录，约 7 天过期。')

    def ready(self):
        if not (config.load().get('igp_token') or '').strip():
            return False, '缺少 iGPSPORT token'
        return True, ''

    def _base(self) -> str:
        return REGIONS.get(config.load().get('igp_region') or 'cn', REGIONS['cn'])

    def _token(self) -> str:
        return (config.load().get('igp_token') or '').strip()

    # ---------------- 拉取 ----------------
    #
    # 链路（实测，见 README）：
    #   1. GET /activity/queryMyActivity?pageNo&pageSize   列表（每页 20~30）
    #   2. GET /activity/getDownloadUrl/{rideId}           拿 OSS 直链
    #      ⚠️ rideId 是**路径参数**；写成 ?rideId= 会 500「服务器异常」（网关兜底）
    #   3. GET {直链} -> 字节内容是 FIT，但 URL 不带扩展名、CT 是 octet-stream，
    #      只能靠文件头第 8~12 字节的 b'.FIT' 判断

    def _get_json(self, url: str, **kw):
        r = net.get(url, headers=_hdr(self._token()), timeout=40, **kw)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def _fit_of(self, base: str, rid) -> bytes | None:
        """取单条活动的原始 FIT 字节。取不到返回 None（该条没有文件或被拒）。"""
        code, j = self._get_json(
            f'{base}/service/web-gateway/web-analyze/activity/getDownloadUrl/{rid}')
        if code >= 300 or (j.get('code') not in (0, None)):
            return None
        d = j.get('data')
        url = d if isinstance(d, str) else (
            (d or {}).get('url') or (d or {}).get('downloadUrl') or '')
        if not url:
            return None
        b = net.get(url, timeout=180)
        raw = b.content or b''
        # 不认扩展名，只认 FIT 头，避免把 OSS 的错误页当数据
        if b.status_code >= 300 or len(raw) < 14 or raw[8:12] != b'.FIT':
            return None
        return raw

    def pull(self, limit: int = 0, log=None) -> list[Ride]:
        from ..fitread import read_fit_bytes

        def L(m):
            if log:
                log(m)

        base = self._base()
        gen = {'户外骑行', '室内骑行', '骑行', '骑行台', 'Ride', 'Cycling'}
        out: list[Ride] = []
        page, size = 1, 20
        while page <= 200:
            code, j = self._get_json(
                f'{base}/service/web-gateway/web-analyze/activity/queryMyActivity',
                params={'pageNo': page, 'pageSize': size, 'reqType': 0,
                        'sort': 1, 'sortType': 1})
            if code >= 300:
                raise RuntimeError(f'iGPSPORT 列表读取失败 HTTP {code}: '
                                   f'{str(j)[:160]}')
            rows = ((j.get('data') or {}).get('rows') or [])
            if not rows:
                break
            for x in rows:
                if limit and len(out) >= limit:
                    return out
                rid = x.get('rideId')
                day = str(x.get('startTime') or '')[:10]
                km = round(float(x.get('rideDistance') or 0) / 1000.0, 2)
                try:
                    raw = self._fit_of(base, rid)
                except Exception as exc:
                    L(f'  取 FIT 失败 rideId={rid}：{exc}')
                    raw = None
                if not raw:
                    L(f'  跳过 rideId={rid}（{day}）—— 平台没有可下载的 FIT')
                    continue
                try:
                    ride = read_fit_bytes(raw, sid=str(rid))
                except Exception as exc:
                    L(f'  跳过 rideId={rid} —— FIT 解析失败：{exc}')
                    continue
                t = str(x.get('title') or '').strip()
                ride.title = '' if t in gen else t
                ride.source = self.key
                out.append(ride)
                time.sleep(0.25)      # 别把下载链接打得太密
            L(f'iGPSPORT 已列出 {len(out)} 条')
            if len(rows) < size:
                break
            page += 1
        return out

    # ---------------- 推送 ----------------

    def push(self, ride: Ride, log=None) -> str:
        if not ride.points:
            raise RuntimeError('没有轨迹点，iGPSPORT 无法生成记录')
        pts = [Point(t=p.t, lat=p.lat, lon=p.lon, ele=p.ele, hr=p.hr, cad=p.cad,
                     power=p.power, speed=p.speed, dist=p.dist) for p in ride.points]
        blob = build_fit(pts, Activity(start=ride.start, distance_m=ride.distance_m,
                                       moving_s=ride.moving_s))
        stamp = ride.start.strftime('%Y%m%d_%H%M') if ride.start else 'ride'
        name = f'{stamp}_{ride.fingerprint}.fit'
        base, token = self._base(), self._token()

        r = net.get(f'{base}/service/sportg/third-party-server/oss/getSignedUrl',
                    params={'fileExtension': '.fit'}, headers=_hdr(token))
        d = _unwrap(r)
        url, oss = d.get('signedUrl'), d.get('ossId')
        if not url or not oss:
            raise RuntimeError(f'取签名失败 HTTP {r.status_code}: {r.text[:120]}')
        net.put_raw(url, blob)
        post = net.post(f'{base}/service/web-gateway/web-analyze/activity/uploadByOss',
                        json={'fileName': name, 'ossName': oss}, headers=_hdr(token))
        if post.status_code >= 300:
            raise RuntimeError(f'登记失败 HTTP {post.status_code}: {post.text[:120]}')
        return name

    # ---------------- 去重 ----------------

    def existing(self, log=None) -> set[str]:
        """用「日期|里程(10m)」做键，因为列表接口只给到日期，没有开始时刻。"""
        base, token = self._base(), self._token()
        seen: set[str] = set()
        page = 1
        while page <= 60:
            r = net.get(f'{base}/service/web-gateway/web-analyze/activity/queryMyActivity',
                        params={'pageNo': page, 'pageSize': 20, 'reqType': 0,
                                'sort': 1, 'sortType': 1}, headers=_hdr(token))
            data = _unwrap(r)
            total_page = 0
            if isinstance(data, dict):
                # data 是分页对象 {pageNo,pageSize,totalPage,totalRows,rows}。
                # 早期版本只认 records/list，漏了 rows —— 结果恒为 0 条，
                # 去重形同虚设（实测后修正）。
                total_page = int(data.get('totalPage') or 0)
                rows = (data.get('rows') or data.get('records')
                        or data.get('list') or [])
            else:
                rows = data or []
            if not rows:
                break
            for x in rows:
                day = str(x.get('startTime') or '').replace('.', '-')[:10]
                km10 = int(round(float(x.get('rideDistance') or 0) / 10.0))
                if day:
                    seen.add(f'{day}|{km10}')
            # 服务端把每页压到 20 条（传 30 也只给 20），所以不能拿请求的
            # pageSize 判断是否最后一页，否则第一页就停 —— 只能看 totalPage。
            if total_page:
                if page >= total_page:
                    break
            elif len(rows) < 20:
                break
            page += 1
            time.sleep(0.2)
        if log:
            log(f'iGPSPORT 云端已有 {len(seen)} 条（按日期+里程比对）')
        return seen

    @staticmethod
    def token_key(ride: Ride) -> str:
        from ..model import BJ
        day = ride.start.astimezone(BJ).strftime('%Y-%m-%d') if ride.start else ''
        return f'{day}|{int(round(ride.distance_m / 10.0))}'
