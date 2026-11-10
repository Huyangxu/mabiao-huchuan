# -*- coding: utf-8 -*-
"""迈金 / 顽鹿（ONELAP）适配器：既能推、也能拉。

接口来源（实测 + 前端 bundle 反查，不是猜的）：
  登录    POST https://www.onelap.cn/api/login
          {"account": 账号, "password": md5(密码)}  -> data[0].{token, refresh_token, userinfo.uid}
  鉴权    请求头 Authorization: <token>（原样，不加 Bearer），
          同时带 Cookie: ouid=..; XSRF-TOKEN=..; OTOKEN=..
  上传    POST https://u.onelap.cn/api/otm/ride_record/upload/fit
          multipart 字段名 **`jilu0`**（批量时 jilu0 / jilu1 / … 按序号命名）。
          依据：日历页「上传」控件的真实请求，前端是 FormData.append(`jilu${i}`, file)。
          成功返回 code=200「全部文件上传成功」，data.success_count / failed_count。
          ⚠️ 页头还残留一个旧上传入口，字段名是 `jilu`（无序号），服务端**不认**，
          会回 422「没有上传文件」。早期曾照抄这段旧代码，误判成「平台封禁」。
  列表    POST https://u.onelap.cn/api/otm/ride_record/capability_list
          {"uid": ..., "page": n}，固定 20 条/页，按 data.data.total 翻页读全。
  详情    GET  https://u.onelap.cn/api/otm/ride_record/analysis/{_id}
          -> data.ridingRecord.{fitUrl, fileKey, durl}
  下载    GET  https://u.onelap.cn/api/otm/ride_record/analysis/fit_content/{key}
          key = **base64(fitUrl)**（前端 api chunk 里
          `btoa(unescape(encodeURIComponent(t)))`），不是记录 id；
          返回 application/octet-stream 的原始 FIT。
          早期按 `?id=` 拼 download/fit、或直接取 durl（fits.rfsvr.net 签名直链）
          都拿不到文件：前者 404、后者 CDN 也回 404。

依据：官网前端 bundle 的 api / recordDownload / details 三个 chunk 反查，并用真实
账号逐条实测（fit_content 返回 282971 字节、头含 .FIT，解析出 9749 个轨迹点）。
上传实测：`jilu0` 成功（code=200，success_count=1），`jilu` 稳定 422。
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.parse import quote

from .. import config, net
from ..fitlib import Activity, Point, build_fit
from ..model import BJ, Ride
from .base import Adapter

WEB = 'https://www.onelap.cn'
UP = 'https://u.onelap.cn'


class OnelapError(RuntimeError):
    pass


class OnelapRejected(OnelapError):
    """顽鹿服务端明确拒绝上传，重试同一批也没用。

    典型是 422「没有上传文件」——通常意味着**字段名或接口改版**（本工具用
    日历页批量入口的 `jilu0`）。遇到它立刻中止整批，别让用户等几分钟看
    200 条一模一样的失败；提示里给出排查方向。
    """


def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode('utf-8')).hexdigest()


class Onelap(Adapter):
    key = 'onelap'
    name = '迈金 顽鹿'
    caps = ('pull', 'push')
    needs = ('onelap_account', 'onelap_password')
    note = ('账号+密码登录。读（原始 FIT 下载）与写（上传 FIT）都已实测；'
            '列表按 total 翻页读全，去重只认云端实况。')

    # existing() 会把云端列表读全后置 True。pipeline 只有在 True 时才敢忽略
    # 本地「已同步」标记、完全按云端实况去重（否则宁可保守，避免重复上传）。
    cloud_complete = False

    # ---------------- 登录与鉴权 ----------------

    def ready(self):
        c = config.load()
        if (c.get('onelap_token') or '').strip():
            return True, ''
        if c.get('onelap_account') and c.get('onelap_password'):
            return False, '已存账号，但还没登录'
        return False, '还没登录顽鹿'

    def _state(self) -> dict:
        c = config.load()
        return {
            'token': (c.get('onelap_token') or '').strip(),
            'refresh': (c.get('onelap_refresh') or '').strip(),
            'uid': str(c.get('onelap_uid') or '').strip(),
        }

    @staticmethod
    def login(account: str, password: str) -> dict:
        """账号密码登录，返回 {token, refresh, uid}。密码按顽鹿前端规则做 MD5。"""
        r = net.post(f'{WEB}/api/login',
                     json={'account': account, 'password': md5_hex(password)},
                     headers={'Content-Type': 'application/json;charset=UTF-8',
                              'Accept': 'application/json'})
        try:
            j = r.json()
        except ValueError:
            raise OnelapError(f'登录返回非 JSON（HTTP {r.status_code}）：{r.text[:160]}')
        if r.status_code >= 300 or j.get('code') not in (200, 0, '200', '0'):
            raise OnelapError(f"顽鹿登录失败：{j.get('error') or j.get('message') or r.text[:160]}")
        rows = j.get('data') or []
        if isinstance(rows, dict):
            rows = [rows]
        if not rows:
            raise OnelapError('顽鹿登录成功但没返回数据')
        d = rows[0]
        info = d.get('userinfo') or d.get('userInfo') or {}
        token = d.get('token') or ''
        if not token:
            raise OnelapError('顽鹿登录没拿到 token')
        return {
            'token': token,
            'refresh': d.get('refresh_token') or d.get('refreshToken') or '',
            'uid': str(info.get('uid') or info.get('id') or d.get('uid') or ''),
        }

    def _hdr(self) -> dict:
        st = self._state()
        h = {
            'Authorization': st['token'],
            'Accept': 'application/json',
            'Origin': WEB,
            'Referer': WEB + '/',
        }
        jar = []
        if st['uid']:
            jar.append(f'ouid={st["uid"]}')
        if st['token']:
            jar.append(f'XSRF-TOKEN={st["token"]}')
        if st['refresh']:
            jar.append(f'OTOKEN={st["refresh"]}')
        if jar:
            h['Cookie'] = '; '.join(jar)
        return h

    def _check(self) -> None:
        if not self._state()['token']:
            raise OnelapError('还没登录顽鹿，请先输入账号密码')

    # ---------------- 推送 ----------------

    def push(self, ride: Ride, log=None) -> str:
        self._check()
        if not ride.points:
            raise RuntimeError('没有轨迹点，顽鹿无法生成记录')
        pts = [Point(t=p.t, lat=p.lat, lon=p.lon, ele=p.ele, hr=p.hr, cad=p.cad,
                     power=p.power, speed=p.speed, dist=p.dist) for p in ride.points]
        blob = build_fit(pts, Activity(start=ride.start, distance_m=ride.distance_m,
                                       moving_s=ride.moving_s))
        stamp = ride.start.strftime('%Y%m%d_%H%M') if ride.start else 'ride'
        name = f'{stamp}_{ride.fingerprint}.fit'

        r = net.multipart(
            f'{UP}/api/otm/ride_record/upload/fit',
            # 字段名带序号！日历页批量上传用 jilu0/jilu1/…；
            # 页头旧入口的 `jilu`（无序号）会被服务端当成「没有上传文件」。
            [{'name': 'jilu0', 'filename': name, 'data': blob,
              'content_type': 'application/octet-stream'}],
            headers=self._hdr())
        if r.status_code >= 300:
            hint = '（token 失效，重新登录）' if r.status_code in (401, 403) else ''
            raise RuntimeError(f'顽鹿上传失败 HTTP {r.status_code}{hint}: {r.text[:200]}')
        try:
            j = r.json()
        except ValueError:
            j = {}
        code = j.get('code') if isinstance(j, dict) else None
        if code not in (None, 0, 200, '0', '200'):
            msg = str(j.get('error') or j.get('message') or r.text[:200])
            body = json.dumps(j, ensure_ascii=False)[:240]
            if str(code) == '422' and ('没有上传文件' in msg or '文件' in msg):
                # 多半是字段名/接口改版：服务端没从 multipart 里找到它要的字段。
                # 整批立刻停，别再逐条重试。
                raise OnelapRejected(
                    f'顽鹿上传接口拒绝（code=422 {msg}）。本工具按日历页批量入口发 '
                    f'`jilu0` 字段；如果顽鹿改了字段名，就会返回这一句。'
                    f'请在顽鹿网页「上传」里抓一次请求核对字段名，或反馈给我们更新。'
                    f'服务端原始返回：{body}')
            raise OnelapError(f'顽鹿拒绝：{msg}（{body}）')
        data = j.get('data') if isinstance(j, dict) else None
        if isinstance(data, dict):
            failed = data.get('failed_count')
            if failed not in (None, 0, '0'):
                raise OnelapError(
                    f'顽鹿有 {failed} 个文件没上传成功：'
                    f'{json.dumps(j, ensure_ascii=False)[:240]}')
        if log:
            log(f'顽鹿已接收：{json.dumps(j, ensure_ascii=False)[:150]}')
        return f'onelap:{name}'

    # ---------------- 拉取 ----------------

    _LIST_BODIES = (
        lambda uid: {'uid': uid},
        lambda uid: {'uid': uid, 'page': 1, 'pageSize': 100},
        lambda uid: {'uid': uid, 'pageNo': 1, 'pageSize': 100},
        lambda uid: {'uid': uid, 'page': 1, 'size': 100, 'pageNo': 1},
    )

    _CAP_PATH = '/api/otm/ride_record/capability_list'
    _CAP_PAGE = 20

    def _capability_rows(self, uid: str, log=None) -> list[dict] | None:
        """官网「运动记录」页列表：data.data.list + total，**固定每页 20 条**。

        只有 `page` 有效，`pageSize` 被服务端忽略。早期账号只有 6 条时看起来像
        「一次给全量」，记录一多就只拿到首页 20 条 —— 去重会
        漏掉 149 条，本地标记又写着「已同步」，结果云端明明没有却判成跳过。
        """
        for base in (UP, WEB):
            try:
                r = net.post(f'{base}{self._CAP_PATH}',
                             json={'uid': uid, 'page': 1}, headers=self._hdr())
                j = r.json()
            except Exception:
                continue
            if not isinstance(j, dict) or j.get('code') not in (200, '200', 0, None):
                continue
            first = _find_rows(j)
            if first is None:
                continue
            d = j.get('data') or {}
            inner = d.get('data') if isinstance(d, dict) else {}
            try:
                total = int(inner.get('total') or 0) if isinstance(inner, dict) else 0
            except (TypeError, ValueError):
                total = 0

            all_rows = list(first)
            seen = {str(x.get('_id') or x.get('id') or '') for x in first}
            seen.discard('')
            pages = max(1, min((total + self._CAP_PAGE - 1) // self._CAP_PAGE, 200))
            if log:
                log(f'顽鹿记录列表（capability_list）第 1 页 {len(first)} 条，total={total}')
            for page in range(2, pages + 1):
                try:
                    rr = net.post(f'{base}{self._CAP_PATH}',
                                  json={'uid': uid, 'page': page}, headers=self._hdr())
                    part = _find_rows(rr.json()) or []
                except Exception as exc:
                    if log:
                        log(f'（顽鹿列表第 {page} 页读取失败，按已读到的 '
                            f'{len(all_rows)} 条去重：{exc}）')
                    break
                if not part:
                    break
                for x in part:
                    sid = str(x.get('_id') or x.get('id') or '')
                    if sid and sid in seen:
                        continue
                    if sid:
                        seen.add(sid)
                    all_rows.append(x)
                if len(part) < self._CAP_PAGE:
                    break
            if log and pages > 1:
                log(f'顽鹿列表已读 {len(all_rows)}/{total} 条（最多 {pages} 页）')
            self.cloud_complete = (total == 0 or len(all_rows) >= total)
            if not self.cloud_complete and log:
                log('（云端列表没读全，本次仍按本地同步标记去重，避免重复上传）')
            return all_rows
        return None

    def _rows(self, log=None) -> list[dict]:
        self._check()
        uid = self._state()['uid']
        try:
            rows = self._capability_rows(uid, log=log)
            if rows is not None:
                return rows
        except Exception as exc:
            if log:
                log(f'（顽鹿 capability_list 读取失败，改用旧列表接口：{exc}）')
        last = ''
        for mk in self._LIST_BODIES:
            body = mk(uid)
            for url in (f'{UP}/api/otm/ride_record/list', f'{WEB}/api/otm/ride_record/list'):
                try:
                    r = net.post(url, json=body, headers=self._hdr())
                except Exception as exc:
                    last = f'{type(exc).__name__}: {exc}'
                    continue
                if r.status_code >= 400:
                    last = f'HTTP {r.status_code} {r.text[:120]}'
                    continue
                try:
                    j = r.json()
                except ValueError:
                    last = f'非 JSON：{r.text[:120]}'
                    continue
                code = j.get('code') if isinstance(j, dict) else None
                if code not in (None, 0, 200, '0', '200'):
                    last = f'接口返回 code={code}：{str(j.get("message") or "")[:80]}'
                    continue
                # 关键：空数组是「云端 0 条记录」的合法成功响应，不是解析失败。
                # 之前 _pick_rows 要求数组非空，遇到 {"data":{"list":[],"pagination":{"total":0}}}
                # 会把 8 种参数全试完再抛错，界面上就显示成「读顽鹿已有记录失败」。
                rows = _find_rows(j)
                if rows is None:
                    last = f'返回里没找到数组：{json.dumps(j, ensure_ascii=False)[:200]}'
                    continue
                if rows:
                    if log:
                        log(f'顽鹿列表命中：{url} body={body} 共 {len(rows)} 条；'
                            f'字段：{sorted(rows[0].keys())[:12]}')
                    return self._more_pages(url, uid, rows, j, log=log)
                total = _resp_total(j)
                if total in (None, 0):
                    if log:
                        log(f'顽鹿列表命中：{url} body={body}，云端暂无记录（total=0）')
                    return []
                last = f'列表是空的但 total={total}，换组参数再试'
        raise OnelapError(f'读顽鹿记录列表失败（最后错误：{last}）')

    @staticmethod
    def _page_count(j, page_size: int = 20) -> int:
        """优先 pagination.total_pages；capability_list 只给 total，按每页条数折算。"""
        def walk(o):
            if not isinstance(o, dict):
                return 0
            p = o.get('pagination') or o.get('pageInfo')
            if isinstance(p, dict):
                for k in ('total_pages', 'totalPages', 'pages'):
                    v = p.get(k)
                    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
                        return int(v)
            for v in o.values():
                n = walk(v)
                if n:
                    return n
            return 0
        n = walk(j)
        if not n:
            total = _resp_total(j)
            if total:
                n = (int(total) + max(page_size, 1) - 1) // max(page_size, 1)
        return min(max(n, 1), 200)

    def _more_pages(self, url: str, uid: str, first: list[dict],
                    first_resp: dict, log=None) -> list[dict]:
        """顽鹿列表固定每页 20 条：`page` 有效、`pageSize` 被忽略。

        只读首页的话，云端超过 20 条之后去重就全瞎了，会重复上传。
        """
        pages = self._page_count(first_resp, len(first) or self._CAP_PAGE)
        all_rows = list(first)
        seen = {str(x.get('_id') or x.get('id') or x.get('rid') or '') for x in first}
        seen.discard('')
        for page in range(2, pages + 1):
            try:
                r = net.post(url, json={'uid': uid, 'page': page, 'pageSize': 100},
                             headers=self._hdr())
                j = r.json()
            except Exception as exc:
                if log:
                    log(f'（顽鹿列表第 {page} 页读取失败，按已读到的去重：{exc}）')
                break
            rows = _find_rows(j) or []
            if not rows:
                break
            for x in rows:
                sid = str(x.get('_id') or x.get('id') or x.get('rid') or '')
                if sid and sid in seen:
                    continue
                if sid:
                    seen.add(sid)
                all_rows.append(x)
        if log and len(all_rows) > len(first):
            log(f'顽鹿列表共 {len(all_rows)} 条（{pages} 页已读完）')
        self.cloud_complete = True
        return all_rows

    def existing(self, log=None) -> set[str]:
        seen: set[str] = set()
        self.cloud_complete = False
        try:
            rows = self._rows(log=log)
        except Exception as exc:
            if log:
                log(f'（读顽鹿已有记录失败，改为只按本地库去重：{exc}）')
            return seen
        for x in rows:
            k = _row_key(x)
            if k:
                seen.add(k)
        if log:
            log(f'顽鹿云端已有 {len(seen)} 条（按日期+里程比对）')
        return seen

    def pull(self, limit: int = 0, log=None) -> list[Ride]:
        rows = self._rows(log=log)
        if limit:
            rows = rows[:limit]
        out: list[Ride] = []
        for i, x in enumerate(rows, 1):
            try:
                ride = self._one(x, log)
            except Exception as exc:
                if log:
                    log(f'[{i}/{len(rows)}] 跳过：{exc}')
                continue
            if ride and ride.points:
                out.append(ride)
                if log:
                    log(f'[{i}/{len(rows)}] {ride.describe()}')
            time.sleep(0.3)
        return out

    def _one(self, row: dict, log=None) -> Ride | None:
        """下载单条原始 FIT 再解析。

        官网前端链路：analysis/{_id} 拿 fitUrl → fit_content/{base64(fitUrl)} 下文件。
        路径参数是 fitUrl 的 base64，不是记录 id；下载用 GET，不是 POST。
        """
        rid = str(_first(row, ('_id', 'id', 'recordId', 'record_id',
                               'rideId', 'ride_id')) or '')
        fit_key = str(_first(row, ('fitUrl', 'fileKey', 'fit_url', 'file_key')) or '')
        durl = str(_first(row, ('durl', 'downloadUrl', 'fileUrl')) or '')

        if rid and (not fit_key or not durl):
            try:
                info = net.get(f'{UP}/api/otm/ride_record/analysis/{rid}',
                               headers=self._hdr()).json()
                rr = ((info.get('data') or {}).get('ridingRecord') or {})
                if not fit_key:
                    fit_key = str(_first(rr, ('fitUrl', 'fileKey',
                                              'fit_url', 'file_key')) or '')
                if not durl:
                    durl = str(rr.get('durl') or '')
                # 详情里的标题/日期更准，补回 row（不覆盖列表已有值）
                for k in ('name', 'title', 'date', 'totalDistance'):
                    if rr.get(k) not in (None, '') and row.get(k) in (None, ''):
                        row = {**row, k: rr[k]}
            except Exception as exc:
                if log:
                    log(f'（顽鹿详情 analysis/{rid} 读取失败：{exc}）')

        if not fit_key:
            raise RuntimeError(
                f'顽鹿这条记录没有 fitUrl（手动录入/第三方同步的记录可能没有原始文件）：{rid}')

        hdr = {**self._hdr(), 'Accept': '*/*'}
        urls = [fit_content_url(fit_key)]
        if durl.startswith('http'):
            urls.append(durl)          # 签名直链当前 404，留作 CDN 恢复后的兜底

        blob = None
        err = ''
        for u in urls:
            try:
                r = net.get(u, headers=hdr)   # 必须 GET：fit_content 不接受 POST
                if r.status_code == 200 and _is_fit(r.content):
                    blob = r.content
                    break
                err = f'{u.split("?")[0][:100]} -> HTTP {r.status_code}'
            except Exception as exc:
                err = f'{u.split("?")[0][:100]} -> {exc}'
        if blob is None:
            raise RuntimeError(f'拿不到 FIT（{err}）')

        from .. import bundle
        fname = fit_key.rsplit('/', 1)[-1] or f'{rid}.fit'
        if not fname.lower().endswith(('.fit', '.gpx', '.tcx')):
            fname += '.fit'
        ride = bundle._read_bytes(fname, blob)
        ride.source = self.key
        ride.sid = rid or fname
        title = str(_first(row, ('name', 'title', 'remark')) or '')
        if title:
            ride.title = title
        return ride

    @staticmethod
    def token_key(ride: Ride) -> str:
        from ..model import BJ
        day = ride.start.astimezone(BJ).strftime('%Y-%m-%d') if ride.start else ''
        return f'{day}|{int(round(ride.distance_m / 10.0))}'


# ---------------- FIT 下载辅助 ----------------

def fit_content_url(fit_key: str) -> str:
    """官网前端：`fit_content/` 后接的是 fitUrl 的 base64，不是记录 id。

    前端 api chunk：downloadFit = GET fit_content/${btoa(unescape(encodeURIComponent(t)))}。
    base64 里的 / 和 + 做 URL 转义后服务端同样接受（实测原始/转义/urlsafe 均可）。
    """
    key = quote(base64.b64encode(fit_key.encode('utf-8')).decode('ascii'), safe='')
    return f'{UP}/api/otm/ride_record/analysis/fit_content/{key}'


def _is_fit(blob: bytes) -> bool:
    """FIT 头：首字节是 header 长度，8~12 字节是 .FIT 魔数。"""
    return (len(blob) > 100
            and (b'.FIT' in blob[:16] or blob[:1] in (b'\x0e', b'\x0c')))


# ---------------- 解析辅助 ----------------

def _find_rows(j) -> list[dict] | None:
    """从各种可能的壳里把记录数组挖出来。

    返回 None = 响应结构里根本没有数组（解析失败）；
    返回 []   = 找到数组但它是空的（合法：账号还没有记录）。
    """
    if isinstance(j, list):
        return [x for x in j if isinstance(x, dict)]
    if not isinstance(j, dict):
        return None
    for k in ('data', 'records', 'rows', 'list', 'items', 'result'):
        if k not in j:
            continue
        v = j.get(k)
        if isinstance(v, list):
            if not v:
                return []
            if isinstance(v[0], dict):
                return [x for x in v if isinstance(x, dict)]
        if isinstance(v, dict):
            inner = _find_rows(v)
            if inner is not None:
                return inner
    return None


def _pick_rows(j) -> list[dict]:
    """兼容老调用：只关心「有没有记录」，空数组当空列表。"""
    return _find_rows(j) or []


def _resp_total(j) -> int | None:
    """翻出 pagination.total / total，用来判断「列表空」是真空还是分页参数不对。"""
    if isinstance(j, dict):
        for k in ('total', 'totalCount', 'totalElements'):
            v = j.get(k)
            if isinstance(v, (int, float)):
                return int(v)
            if isinstance(v, str) and v.isdigit():
                return int(v)
        for k in ('pagination', 'page', 'meta', 'data'):
            v = j.get(k)
            if isinstance(v, dict):
                t = _resp_total(v)
                if t is not None:
                    return t
    return None


def _first(d: dict, keys) -> object:
    for k in keys:
        if d.get(k) not in (None, ''):
            return d[k]
    return None


def _row_key(x: dict) -> str:
    """日期|里程(10m)，口径必须和 Onelap.token_key 完全一致。

    顽鹿列表真实字段是 `start_riding_time` + `distance_km`（公里）；
    旧版只认 `startTime` + `distance`（米），每条都算出空键，
    结果「云端已有 0 条」——去重形同虚设，全量跑一遍就会重复上传。
    """
    raw = _first(x, ('date', 'start_riding_time', 'startRidingTime', 'startTime',
                     'start_time', 'beginTime', 'begin_time',
                     'startTimeLocal', 'time', 'timestamp'))
    day = ''
    if isinstance(raw, (int, float)) and raw > 1e9:
        ts = raw / 1000 if raw > 1e11 else raw
        day = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(BJ).strftime('%Y-%m-%d')
    elif raw:
        day = str(raw)[:10].replace('/', '-').replace('.', '-')
    if len(day) < 10:
        return ''
    try:
        # 米：/10 得到十米；公里：*100 得到十米
        dist_m = _first(x, ('distance', 'rideDistance', 'ride_distance',
                            'totalDistance', 'total_distance', 'mileage'))
        if dist_m not in (None, ''):
            return f'{day}|{int(round(float(dist_m) / 10.0))}'
        km = _first(x, ('distance_km', 'distanceKm', 'total_distance_km', 'km'))
        if km not in (None, ''):
            return f'{day}|{int(round(float(km) * 100))}'
    except (TypeError, ValueError):
        return ''
    return ''
