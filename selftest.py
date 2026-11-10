# -*- coding: utf-8 -*-
"""离线自测：不联网，验证 FIT/GPX/TCX 往返、指纹稳定、入库去重、导出可回读。

    python selftest.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 用临时数据目录，避免污染真实库
os.environ['METERHUB_HOME'] = tempfile.mkdtemp(prefix='meterhub_test_')

from meterhub import bundle, store                      # noqa: E402
from meterhub.fitlib import Activity, Point, build_fit  # noqa: E402
from meterhub.fitread import read_fit                   # noqa: E402
from meterhub.gpxio import read_gpx, read_tcx, write_gpx, write_tcx  # noqa: E402
from meterhub.model import Ride, RidePoint              # noqa: E402

FAIL = []


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        FAIL.append(name)


def sample(n=300) -> Ride:
    t0 = datetime(2026, 5, 1, 1, 30, tzinfo=timezone.utc)
    pts = []
    dist = 0.0
    for i in range(n):
        step = 8.0 + (i % 7)
        dist += step
        pts.append(RidePoint(
            t=t0 + timedelta(seconds=i * 3),
            lat=30.5000 + i * 0.00006, lon=114.3000 + i * 0.000055,
            ele=22.0 + (i % 40) * 0.5, hr=118 + (i % 25), cad=82 + (i % 9),
            power=150 + (i % 60), speed=step / 3, dist=dist,
        ))
    r = Ride(source='test', sid='t1', title='自测骑行', start=pts[0].t, points=pts,
             distance_m=dist, moving_s=(n - 1) * 3, elapsed_s=(n - 1) * 3 + 120)
    return r.fix_totals()


def main() -> int:
    print('meterhub 离线自测')

    r = sample()
    fp0 = r.fingerprint
    print(f"\n[1] 样本：{r.describe()}  fp={fp0}")

    # ---- FIT 往返 ----
    pts = [Point(t=p.t, lat=p.lat, lon=p.lon, ele=p.ele, hr=p.hr, cad=p.cad,
                 power=p.power, speed=p.speed, dist=p.dist) for p in r.points]
    blob = build_fit(pts, Activity(start=r.start, distance_m=r.distance_m, moving_s=r.moving_s))
    tmp = Path(tempfile.mkdtemp(prefix='mh_fit_'))
    f = tmp / 'a.fit'
    f.write_bytes(blob)
    back = read_fit(f)
    print(f"[2] FIT 回读：{back.describe()}  bytes={len(blob)}")
    check('FIT 点数一致', len(back.points) == len(r.points), f'{len(back.points)} vs {len(r.points)}')
    check('FIT 开始时间一致', abs((back.start - r.start).total_seconds()) < 1)
    check('FIT 里程一致', abs(back.distance_m - r.distance_m) < 1.0,
          f'{back.distance_m:.1f} vs {r.distance_m:.1f}')
    check('FIT 心率保留', back.points[10].hr == r.points[10].hr)
    check('FIT 功率保留', back.points[10].power == r.points[10].power)
    check('FIT 海拔保留', abs((back.points[10].ele or 0) - (r.points[10].ele or 0)) < 1)
    check('FIT 指纹稳定', back.fingerprint == fp0, f'{back.fingerprint} vs {fp0}')

    # ---- GPX 往返 ----
    g = tmp / 'a.gpx'
    g.write_bytes(write_gpx(r))
    rb = read_gpx(g)
    print(f"[3] GPX 回读：{rb.describe()}")
    check('GPX 点数一致', len(rb.points) == len(r.points))
    check('GPX 心率保留', rb.points[10].hr == r.points[10].hr)
    check('GPX 时间保留', abs((rb.points[10].t - r.points[10].t).total_seconds()) < 1)

    # ---- TCX 往返 ----
    c = tmp / 'a.tcx'
    c.write_bytes(write_tcx(r))
    rc = read_tcx(c)
    print(f"[4] TCX 回读：{rc.describe()}")
    check('TCX 点数一致', len(rc.points) == len(r.points))
    check('TCX 功率保留', rc.points[10].power == r.points[10].power)

    # ---- 入库/去重 ----
    fp, new1 = store.add(r)
    fp2, new2 = store.add(r)
    print(f"[5] 入库：{fp} new={new1} / 重复入库 new={new2}")
    check('首次入库成功', new1 is True)
    check('重复入库被去重', new2 is False)
    check('库内条数为 1', store.stats()['count'] == 1)

    loaded = store.get(fp)
    check('库里能读回', loaded is not None and len(loaded.points) == len(r.points))

    # ---- 批量导入：文件夹 + zip ----
    import zipfile
    inbox = tmp / 'inbox'
    inbox.mkdir()
    (inbox / 'a.fit').write_bytes(blob)
    (inbox / 'b.gpx').write_bytes(write_gpx(r))
    z = tmp / 'pack.zip'
    with zipfile.ZipFile(z, 'w') as zf:
        zf.write(f, 'export/ride1.fit')
        zf.write(g, 'export/ride1.gpx')
    rides_dir = bundle.collect(inbox)
    rides_zip = bundle.collect(z)
    print(f"[6] 批量：文件夹 {len(rides_dir)} 条，zip {len(rides_zip)} 条")
    check('文件夹扫描到 2 个文件', len(rides_dir) == 2)
    check('zip 扫描到 2 个文件', len(rides_zip) == 2)

    # ---- 导出并回读 ----
    out = tmp / 'outbox'
    written = bundle.export_all([r], out, fmt='fit')
    check('导出文件存在', written and written[0].exists())
    rb2 = read_fit(written[0])
    check('导出后可回读且指纹一致', rb2.fingerprint == fp0)
    print(f"[7] 导出：{written[0].name}")

    # ---- OAuth1 签名（佳明登录用的，必须是标准 OAuth 1.0a 形态）----
    from meterhub import garmin_auth
    params = {'oauth_consumer_key': 'cs', 'oauth_nonce': 'n1',
              'oauth_signature_method': 'HMAC-SHA1', 'oauth_timestamp': '137131201',
              'oauth_version': '1.0', 'b': '2', 'a': '1'}
    import base64
    import hashlib
    import hmac
    # 期望值用"手写的规范基串"独立算一遍，验证排序/编码/拼接都对
    base = 'GET&http%3A%2F%2Fx.com%2Fa&a%3D1%26b%3D2%26oauth_consumer_key%3Dcs%26' \
           'oauth_nonce%3Dn1%26oauth_signature_method%3DHMAC-SHA1%26' \
           'oauth_timestamp%3D137131201%26oauth_version%3D1.0'
    want = base64.b64encode(hmac.new(b'cs&', base.encode(), hashlib.sha1).digest()).decode()
    got = garmin_auth.signature('GET', 'http://x.com/a?ignored=1', params, 'cs')
    print('[8] OAuth1 签名')
    check('签名与规范基串一致', got == want, got[:12] + '…')
    hdr = garmin_auth._enc('中文 空格')
    check('参数编码符合 RFC3986', hdr == '%E4%B8%AD%E6%96%87%20%E7%A9%BA%E6%A0%BC', hdr)

    # ---- 顽鹿鉴权与上传参数 ----
    from meterhub.adapters.onelap import Onelap, _pick_rows, _row_key, md5_hex
    print('[9] 顽鹿')
    check('密码 MD5 正确', md5_hex('abc123') == 'e99a18c428cb38d5f260853678922e03')
    check('能解析 data 壳', len(_pick_rows({'data': [{'id': 1}]})) == 1)
    check('能解析嵌套壳', len(_pick_rows({'data': {'records': [{'id': 2}]}})) == 1)
    check('日期里程键（米）', _row_key({'startTime': '2026-05-01 09:30:00',
                                       'distance': 32970}) == '2026-05-01|3297')
    check('日期里程键（顽鹿真实字段 start_riding_time + distance_km）',
          _row_key({'start_riding_time': '2024-03-26 17:43:02',
                    'distance_km': 50.97}) == '2024-03-26|5097')
    check('顽鹿去重键与 token_key 同口径',
          _row_key({'start_riding_time': '2026-05-01 09:30:00',
                    'distance_km': 32.97}) == '2026-05-01|3297')
    check('capability_list 字段（date + totalDistance）能算出去重键',
          _row_key({'date': '2026-05-02', 'totalDistance': 43433.53,
                    'startRidingTime': 1777718400000}) == '2026-05-02|4343')
    check('没有有效日期/里程时返回空键',
          _row_key({'date': None, 'created_at': '1970-01-01 08:00:00'}) == '')
    _ = Onelap()
    # 真实账号 0 条记录时的返回：list 是空数组、pagination.total=0。
    # 旧版把「空数组」当成「没找到数组」，界面上就会报「读顽鹿已有记录失败」。
    from meterhub.adapters import onelap as OL
    empty = {'code': 200, 'message': '查询成功',
             'data': {'list': [], 'pagination': {'total_pages': 0, 'has_more': False,
                                                 'current_page': 1, 'per_page': 20,
                                                 'total': 0}}}
    check('顽鹿空列表被识别成合法返回',
          OL._find_rows(empty) == [] and OL._resp_total(empty) == 0)
    check('真正的异常结构仍识别为 None',
          OL._find_rows({'code': 200, 'message': '成功'}) is None)

    class _FakeResp:
        status_code = 200
        text = ''
        def json(self):
            return empty

    old_state, old_post = OL.Onelap._state, OL.net.post
    OL.Onelap._state = lambda self: {'token': 't', 'refresh': '', 'uid': '1'}
    OL.net.post = lambda *a, **k: _FakeResp()
    try:
        check('顽鹿 0 条记录不再抛「列表失败」', OL.Onelap()._rows() == [])
    finally:
        OL.Onelap._state, OL.net.post = old_state, old_post

    # 顽鹿固定 20 条/页，只读首页会漏掉后面的记录 → 全量跑会重复上传
    class _PagedResp:
        status_code = 200
        text = ''
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p
    calls = []
    def _paged_post(url, json=None, headers=None):
        if 'capability_list' in url:          # 强制走旧列表接口的分页分支
            return _PagedResp({'code': 500, 'message': 'mock unavailable'})
        page = (json or {}).get('page', 1)
        calls.append(page)
        row = {'id': 'a' if page == 1 else 'b',
               'start_riding_time': f'2026-01-0{page} 08:00:00',
               'distance_km': 10 * page}
        return _PagedResp({'code': 200, 'data': {
            'list': [row], 'pagination': {'total_pages': 2, 'per_page': 20, 'total': 21}}})
    old_state3, old_post3 = OL.Onelap._state, OL.net.post
    OL.Onelap._state = lambda self: {'token': 't', 'refresh': '', 'uid': '1'}
    OL.net.post = _paged_post
    try:
        got = OL.Onelap()._rows()
        check('顽鹿列表会翻页读完', len(got) == 2 and got[1]['id'] == 'b')
        check('分页请求确实发到第 2 页', 2 in calls)
    finally:
        OL.Onelap._state, OL.net.post = old_state3, old_post3

    # 官网真列表 capability_list 只给 total、不给 total_pages，固定 20 条/页。
    # 云端记录超过一页时，只读首页会漏掉后面的记录；本地若又标着「已同步」，
    # 云端明明没有的记录就会被误判成「目标已有，跳过」，怎么点都补不回来。
    def _cap_payload(page, total=45):
        n = 20 if page < 3 else 5
        rows = [{'_id': f'id{page}_{i}', 'date': f'2026-01-{page:02d}',
                 'totalDistance': 10000 + i * 137} for i in range(n)]
        return {'code': 200, 'data': {'error': 'success',
                                      'data': {'list': rows, 'total': total}}}
    cap_calls = []
    def _cap_post(url, json=None, headers=None):
        if 'capability_list' not in url:
            return _PagedResp({'code': 500, 'message': 'mock unavailable'})
        page = (json or {}).get('page', 1)
        cap_calls.append(page)
        return _PagedResp(_cap_payload(page))
    old_state4, old_post4 = OL.Onelap._state, OL.net.post
    OL.Onelap._state = lambda self: {'token': 't', 'refresh': '', 'uid': '1'}
    OL.net.post = _cap_post
    try:
        o = OL.Onelap()
        got = o._capability_rows('1')
        check('capability_list 按 total 翻页读全 45 条', len(got) == 45)
        check('capability_list 确实翻到第 2/3 页', 2 in cap_calls and 3 in cap_calls)
        check('列表读全后 cloud_complete=True', o.cloud_complete is True)
        check('_page_count 能用 total 折算页数',
              OL.Onelap._page_count({'data': {'data': {'total': 45}}}) == 3)
    finally:
        OL.Onelap._state, OL.net.post = old_state4, old_post4

    # 上传字段名：日历页批量入口是 jilu0/jilu1…，页头旧入口的 jilu 会回 422
    import json as _json
    class _FakeUp:
        def __init__(self, payload):
            self.status_code = 200
            self.text = _json.dumps(payload, ensure_ascii=False)
            self._p = payload
        def json(self):
            return self._p
    old_mp, old_state2 = OL.net.multipart, OL.Onelap._state
    seen = {}
    def _mp(url, parts, headers=None):
        seen['field'], seen['url'] = parts[0]['name'], url
        return seen['resp']
    OL.net.multipart = _mp
    OL.Onelap._state = lambda self: {'token': 't', 'refresh': '', 'uid': '1'}
    try:
        seen['resp'] = _FakeUp({'code': 200, 'message': '全部文件上传成功',
                                'data': {'total_files': 1, 'success_count': 1,
                                         'failed_count': 0}})
        OL.Onelap().push(sample(3))
        check('顽鹿上传字段名是 jilu0', seen.get('field') == 'jilu0')
        check('顽鹿上传 URL 正确',
              seen.get('url', '').endswith('/api/otm/ride_record/upload/fit'))
        seen['resp'] = _FakeUp({'code': 200, 'message': '部分失败',
                                'data': {'total_files': 1, 'success_count': 0,
                                         'failed_count': 1}})
        try:
            OL.Onelap().push(sample(3))
            check('failed_count>0 应判失败', False)
        except OL.OnelapError:
            check('failed_count>0 应判失败', True)
        seen['resp'] = _FakeUp({'code': 422, 'message': '没有上传文件', 'data': []})
        try:
            OL.Onelap().push(sample(3))
            check('顽鹿 422 抛 OnelapRejected', False)
        except OL.OnelapRejected as exc:
            check('顽鹿 422 抛 OnelapRejected 且提示排查字段名',
                  'jilu0' in str(exc))
    finally:
        OL.net.multipart, OL.Onelap._state = old_mp, old_state2

    # ---- 行者扫码：内容格式 + 二维码渲染（不发网络请求）----
    from meterhub import login as LG
    print('[11] 行者扫码登录')
    fake = 'a' * 32
    content = LG.XZ_SCHEME + fake
    check('二维码内容用 imxingzhe scheme',
          content == 'imxingzhe://app.imxingzhe.com/login/confirm?token=' + fake, content[:46] + '…')
    uri = LG.qr_svg(content)
    check('二维码是 SVG data URI', uri.startswith('data:image/svg+xml'), uri[:34])
    check('二维码体积合理', 800 < len(uri) < 12000, f'{len(uri)} 字节')
    from meterhub._vendor import segno
    _q = segno.make(content, error='m')
    check('二维码能编码', _q.symbol_size()[0] >= 21, str(_q.symbol_size()))
    # 错误码翻译齐全（4001/4002/4004/4005）
    check('错误码都有中文解释', all(k in LG.XZ_ERR for k in (4001, 4002, 4004, 4005)))
    # 短信入口必须已经拿掉，避免界面再给一个必然失败的入口
    check('短信函数已移除', not hasattr(LG, 'xz_send') and not hasattr(LG, 'xz_login'))

    # ---- 四个方向的能力矩阵 ----
    from meterhub import adapters as A
    print('[12] 方向可达性（四家码表互相传）')
    # 不写死方向，按能力穷举：能读的源 × 能写的目标（排除自己）
    core = ['xingzhe', 'igpsport', 'onelap', 'garmin']
    pairs = [(a, b) for a in core for b in core
             if a != b
             and 'pull' in A.get(a).caps and 'push' in A.get(b).caps]
    for src, tgt in pairs:
        s, t = A.get(src), A.get(tgt)
        check(f'{s.name} → {t.name}', 'pull' in s.caps and 'push' in t.caps)
    check(f'共 {len(pairs)} 个方向（应为 9：4 家互传去掉「→行者」和自身）',
          len(pairs) == 9, str(len(pairs)))
    # 行者没有上传接口，所以任何平台都推不进行者，这是能力边界不是 bug
    check('行者不支持写入（没有上传接口）', 'push' not in A.get('xingzhe').caps)

    # ---- 暂停 / 继续 / 停止 ----
    from meterhub import pipeline as P
    print('[13] 运行控制（暂停 / 继续 / 停止）')
    c = P.RunControl()
    c.checkpoint()
    check('默认可运行', not c.paused and not c.stopped)
    c.pause(); check('暂停生效', c.paused)
    c.resume(); check('继续生效', not c.paused)
    c.stop()
    try:
        c.checkpoint()
        check('停止会中断运行', False)
    except P.StopRequested:
        check('停止会中断运行', c.stopped)
    # 适配器里到处是 except Exception，停止信号必须能穿透它
    swallowed = True
    try:
        try:
            c.checkpoint()
        except Exception:
            pass
    except P.StopRequested:
        swallowed = False
    check('停止信号不会被 except Exception 吞掉', not swallowed)
    import threading as _th
    c2 = P.RunControl()
    c2.pause()
    done = []
    def _worker():
        try:
            c2.checkpoint()
            done.append('resumed')
        except P.StopRequested:
            done.append('stopped')
    t = _th.Thread(target=_worker)
    t.start()
    time.sleep(0.02)
    c2.stop()
    t.join(1)
    check('暂停等待中也能被「停止」唤醒', done == ['stopped'] and not t.is_alive())

    print()
    if FAIL:
        print(f'✗ {len(FAIL)} 项失败：{", ".join(FAIL)}')
        return 1
    print('✓ 全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
