# -*- coding: utf-8 -*-
"""Garmin（佳明）适配器：可以把 FIT 直接推进 Garmin Connect。

为什么能做（实测）：
  * 登录：移动端 SSO 接口还开着，邮箱+密码 (+ MFA 验证码) 能换到 OAuth2 token，
    见 garmin_auth.py。网页端 SSO 已改成 SPA+滑块，那条路走不通。
  * 上传：POST https://connectapi.{domain}/upload-service/upload
    multipart 字段名 `file`，认证 Bearer。返回里 detailedImportResult 带活动 id。
    上传后 Garmin 会异步解析 FIT，几秒后活动才出现在列表里。

能力边界：
  * 只能推，不能拉。拉原始 FIT 要 /download-service/files/activity/{id}，
    本次四个方向都不需要佳明当来源，先不做。
  * FIT 里时间用 UTC，Garmin 按账号时区显示；经纬度用 semicircles。
  * 单文件上限 50 MB、少于 99999 个点（官方限制），本工具生成的 FIT 远小于此。
"""
from __future__ import annotations

import time

from .. import config, garmin_auth, net
from ..fitlib import Activity, Point, build_fit
from ..model import Ride
from .base import Adapter, AlreadyExists


def _fit_from_any(blob: bytes) -> bytes | None:
    """佳明下载接口给的是 zip（里面才有 .fit），也可能直接给 FIT。

    统一取出 FIT 字节；取不到返回 None，让上层跳过这条。
    """
    if len(blob) >= 14 and blob[8:12] == b'.FIT':
        return blob
    if blob[:2] == b'PK':
        import io
        import zipfile
        try:
            z = zipfile.ZipFile(io.BytesIO(blob))
        except Exception:
            return None
        for n in z.namelist():
            if n.lower().endswith('.fit'):
                try:
                    return z.read(n)
                except Exception:
                    return None
    return None


class Garmin(Adapter):
    key = 'garmin'
    name = 'Garmin 佳明'
    caps = ('pull', 'push')
    needs = ('garmin_email', 'garmin_password')
    note = ('手机号（中国站账号就是手机号，也可填邮箱）+密码登录（要验证码会提示输入）。'
            '读 = 下载云端原始 FIT；写 = 直推 FIT，上传后要几秒解析才显示。')

    def ready(self):
        c = config.load()
        if garmin_auth.load_tokens():
            return True, ''
        if c.get('garmin_email') and c.get('garmin_password'):
            return False, '已存账号，但还没换到 token（点「登录」）'
        return False, '还没登录佳明'

    # ---------------- 拉取 ----------------
    #
    # 链路（实测，garmin.cn）：
    #   1. GET connectapi.{domain}/activitylist-service/activities/search/activities
    #      ?start=0&limit=20   -> [{activityId, activityName, distance, startTimeLocal, ...}]
    #   2. GET connectapi.{domain}/download-service/files/activity/{activityId}
    #      -> **zip 包**（CT: application/x-zip-compressed），里面才是 .fit
    # 只认 FIT：手动录入/部分室内活动没有原始文件，下载会 404，跳过即可。

    def pull(self, limit: int = 0, log=None) -> list[Ride]:
        import io
        import zipfile

        from ..fitread import read_fit_bytes

        def L(m):
            if log:
                log(m)

        domain, token = garmin_auth.access_token(log=log)
        H = {'Authorization': f'Bearer {token}', 'Accept': 'application/json',
             'User-Agent': 'GCM-iOS-5.22.1.4'}
        out: list[Ride] = []
        start, size = 0, 20
        while start < 5000:
            r = net.get(
                f'https://connectapi.{domain}/activitylist-service/activities/search/activities',
                params={'start': start, 'limit': size}, headers=H, timeout=60)
            if r.status_code >= 300:
                raise RuntimeError(f'佳明活动列表读取失败 HTTP {r.status_code}: '
                                   f'{r.text[:160]}')
            acts = r.json() or []
            if not acts:
                break
            for a in acts:
                if limit and len(out) >= limit:
                    return out
                aid = a.get('activityId')
                name = str(a.get('activityName') or '').strip()
                try:
                    rb = net.get(
                        f'https://connectapi.{domain}/download-service/files/activity/{aid}',
                        headers={**H, 'Accept': '*/*'}, timeout=180)
                except Exception as exc:
                    L(f'  下载活动 {aid} 失败：{exc}')
                    continue
                if rb.status_code >= 300 or not rb.content:
                    L(f'  跳过活动 {aid}（{name}）—— 平台没有可下载的原始文件')
                    continue
                raw = _fit_from_any(rb.content)
                if not raw:
                    L(f'  跳过活动 {aid}（{name}）—— 包里没有 FIT')
                    continue
                try:
                    ride = read_fit_bytes(raw, sid=str(aid))
                except Exception as exc:
                    L(f'  跳过活动 {aid} —— FIT 解析失败：{exc}')
                    continue
                # 佳明的活动名是「城市 + 运动类型」，比目标平台默认命名更有信息量，保留
                ride.title = name
                ride.source = self.key
                out.append(ride)
                time.sleep(0.2)
            L(f'佳明已列出 {len(out)} 条')
            if len(acts) < size:
                break
            start += size
        return out

    # ---------------- 推送 ----------------

    def push(self, ride: Ride, log=None) -> str:
        if not ride.points:
            raise RuntimeError('没有轨迹点，佳明无法生成活动记录')
        pts = [Point(t=p.t, lat=p.lat, lon=p.lon, ele=p.ele, hr=p.hr, cad=p.cad,
                     power=p.power, speed=p.speed, dist=p.dist) for p in ride.points]
        blob = build_fit(pts, Activity(start=ride.start, distance_m=ride.distance_m,
                                       moving_s=ride.moving_s))
        stamp = ride.start.strftime('%Y%m%d_%H%M') if ride.start else 'ride'
        name = f'{stamp}_{ride.fingerprint}.fit'

        domain, token = garmin_auth.access_token(log=log)
        url = f'https://connectapi.{domain}/upload-service/upload'
        r = net.multipart(
            url,
            [{'name': 'file', 'filename': name, 'data': blob,
              'content_type': 'application/octet-stream'}],
            headers={'Authorization': f'Bearer {token}',
                     'User-Agent': 'GCM-iOS-5.22.1.4'})
        if r.status_code >= 300:
            body = r.text or ''
            low = body.lower()
            # 409 + duplicate：佳明判定「这条活动已经存在」。这是正常情况
            # （以前用网页/App 传过），记为跳过而不是失败。
            if r.status_code in (409, 400) and any(
                    k in low for k in ('duplicate', 'already', '已存在')):
                raise AlreadyExists('佳明已有这条活动，跳过')
            hint = ''
            if r.status_code == 401:
                hint = '（token 被拒，重新登录一次）'
            # 错误正文留长一点，不然只看到半截 JSON 无从判断
            raise RuntimeError(f'上传失败 HTTP {r.status_code}{hint}: {body[:600]}')

        try:
            res = r.json().get('detailedImportResult') or {}
        except ValueError:
            raise RuntimeError(f'上传返回非 JSON：{r.text[:200]}')
        bad = res.get('failures') or []
        if bad:
            msg = bad[0].get('messages') or bad[0]
            raise RuntimeError(f'佳明解析失败：{str(msg)[:200]}')
        succ = res.get('successes') or []
        aid = (succ[0] or {}).get('internalId') if succ else None
        if log:
            log(f'佳明已接收（uploadId={res.get("uploadId")}），活动 id={aid}；'
                f'Garmin 侧解析需要几秒')

        if aid and ride.title:
            self._rename(str(aid), ride.title, log)
        return f'activity:{aid or name}'

    @staticmethod
    def _rename(aid: str, title: str, log=None) -> None:
        """上传后活动名默认是文件名，改回行者里的标题。失败不影响主流程。"""
        try:
            garmin_auth.api('PUT', f'/activity-service/activity/{aid}',
                            json={'activityId': aid, 'activityName': title[:60]})
        except Exception as exc:
            if log:
                log(f'（改名失败，不影响记录：{exc}）')

    # ---------------- 去重 ----------------

    def existing(self, log=None) -> set[str]:
        """用「日期|里程(10m)」做键，和 iGPSPORT 保持同一套比对口径。"""
        seen: set[str] = set()
        start = 0
        while start < 3000:
            rows = garmin_auth.api(
                'GET', '/activitylist-service/activities/search/activities',
                params={'limit': 100, 'start': start}) or []
            if not isinstance(rows, list) or not rows:
                break
            for x in rows:
                day = str(x.get('startTimeLocal') or '')[:10]
                km10 = int(round(float(x.get('distance') or 0) / 10.0))
                if day:
                    seen.add(f'{day}|{km10}')
            if len(rows) < 100:
                break
            start += 100
            time.sleep(0.2)
        if log:
            log(f'佳明云端已有 {len(seen)} 条（按日期+里程比对）')
        return seen

    @staticmethod
    def token_key(ride: Ride) -> str:
        from ..model import BJ
        day = ride.start.astimezone(BJ).strftime('%Y-%m-%d') if ride.start else ''
        return f'{day}|{int(round(ride.distance_m / 10.0))}'
