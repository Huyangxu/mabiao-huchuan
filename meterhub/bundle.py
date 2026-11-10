# -*- coding: utf-8 -*-
"""批量导入：把一个文件夹、一个压缩包（Garmin/Strava/黑鸟/迈金的导出包）、
或单个文件，展开成一堆 Ride。

真实导出包的结构各不相同，所以这里不猜目录，只递归找 .fit/.gpx/.tcx：
  * Garmin 批量导出：DI_CONNECT/DI-Connect-Fitness/*.fit
  * Strava 批量导出：activities/*.fit（或 .gpx/.tcx）
  * 码表 SD 卡：GARMIN/ACTIVITY/*.fit、iGPSPORT/…/*.fit
  * 各家 App 分享出来的单文件
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

from .gpxio import read_any
from .model import Ride

EXTS = ('.fit', '.gpx', '.tcx')


def _read_bytes(name: str, blob: bytes) -> Ride:
    tmp = io.BytesIO(blob)
    if name.lower().endswith('.fit'):
        from .fitread import read_fit
        # read_fit 吃路径，这里给个临时文件
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.fit', delete=False) as f:
            f.write(blob)
            p = f.name
        try:
            r = read_fit(p)
        finally:
            Path(p).unlink(missing_ok=True)
        r.title = Path(name).stem
        r.sid = Path(name).stem
        return r
    if name.lower().endswith('.gpx'):
        import tempfile
        from .gpxio import read_gpx
        with tempfile.NamedTemporaryFile(suffix='.gpx', delete=False) as f:
            f.write(blob)
            p = f.name
        try:
            r = read_gpx(p)
        finally:
            Path(p).unlink(missing_ok=True)
        return r
    if name.lower().endswith('.tcx'):
        import tempfile
        from .gpxio import read_tcx
        with tempfile.NamedTemporaryFile(suffix='.tcx', delete=False) as f:
            f.write(blob)
            p = f.name
        try:
            r = read_tcx(p)
        finally:
            Path(p).unlink(missing_ok=True)
        return r
    raise ValueError(f'不支持的格式：{name}')


def collect(path: str | Path, log=None) -> list[Ride]:
    """从路径收集所有可读的骑行记录。返回的 Ride.source 标记为 file。"""
    p = Path(path)
    rides: list[Ride] = []
    bad: list[str] = []

    def note(msg):
        if log:
            log(msg)

    if p.is_dir():
        files = [f for f in p.rglob('*') if f.suffix.lower() in EXTS and f.is_file()]
        note(f'文件夹里找到 {len(files)} 个轨迹文件')
        for f in files:
            try:
                rides.append(read_any(f))
            except Exception as e:
                bad.append(f'{f.name}: {e}')
    elif p.suffix.lower() == '.zip':
        with zipfile.ZipFile(p) as z:
            names = [n for n in z.namelist() if n.lower().endswith(EXTS)]
            note(f'压缩包里找到 {len(names)} 个轨迹文件')
            for n in names:
                try:
                    rides.append(_read_bytes(n, z.read(n)))
                except Exception as e:
                    bad.append(f'{Path(n).name}: {e}')
    elif p.suffix.lower() in EXTS:
        rides.append(read_any(p))
    else:
        raise ValueError('只支持 .fit/.gpx/.tcx 文件、文件夹或 .zip 包')

    for r in rides:
        r.source = 'file'
        if not r.title:
            r.title = r.sid
    if bad:
        note(f'{len(bad)} 个文件读失败，例如：{bad[0]}')
    return rides


def export_all(rides: list[Ride], out_dir: str | Path, fmt: str = 'fit') -> list[Path]:
    """把 Ride 列表写成文件（用于「导出成可上传的包」）。"""
    from .fitlib import Activity, Point, build_fit
    from .gpxio import write_gpx, write_tcx

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for r in rides:
        stamp = (r.start.strftime('%Y%m%d_%H%M') if r.start else 'unknown')
        base = f'{stamp}_{r.fingerprint}'
        if fmt == 'fit':
            pts = [Point(t=p.t, lat=p.lat, lon=p.lon, ele=p.ele, hr=p.hr, cad=p.cad,
                         power=p.power, speed=p.speed, dist=p.dist) for p in r.points]
            blob = build_fit(pts, Activity(start=r.start, distance_m=r.distance_m,
                                           moving_s=r.moving_s))
            dst = out / f'{base}.fit'
        elif fmt == 'gpx':
            blob = write_gpx(r)
            dst = out / f'{base}.gpx'
        elif fmt == 'tcx':
            blob = write_tcx(r)
            dst = out / f'{base}.tcx'
        else:
            raise ValueError('fmt 只能是 fit / gpx / tcx')
        dst.write_bytes(blob)
        written.append(dst)
    return written
