# -*- coding: utf-8 -*-
"""GPX / TCX 读写（纯标准库）。

为什么要有这一层：不少码表/平台只接受 GPX 或 TCX 上传，或者只提供这两种格式导出。
FIT 是能力最强的（有心率/踏频/功率），但互换链条里必须能退化到 GPX/TCX。
GPX 的扩展命名空间各家不同，读的时候按「本地名」匹配，避免被前缀坑死。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .model import BJ, Ride, RidePoint


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _text(el, *names) -> str:
    for child in el:
        if _local(child.tag) in names and (child.text or '').strip():
            return child.text.strip()
    return ''


def _dig(el, *names):
    """按路径逐层取名，用于 tcx 的 HeartRateBpm/Value 这类嵌套。"""
    cur = el
    for n in names:
        nxt = None
        for child in cur:
            if _local(child.tag) == n:
                nxt = child
                break
        if nxt is None:
            return None
        cur = nxt
    return cur


def read_gpx(path: str | Path) -> Ride:
    root = ET.parse(str(path)).getroot()
    ride = Ride(source='gpx', sid=Path(path).stem, title=Path(path).stem)
    pts: list[RidePoint] = []
    for trkpt in root.iter():
        if _local(trkpt.tag) != 'trkpt':
            continue
        try:
            lat = float(trkpt.get('lat'))
            lon = float(trkpt.get('lon'))
        except (TypeError, ValueError):
            continue
        t_raw = _text(trkpt, 'time')
        try:
            t = datetime.fromisoformat(t_raw.replace('Z', '+00:00'))
        except ValueError:
            continue
        ele = _text(trkpt, 'ele')
        hr = cad = power = None
        for el in trkpt.iter():
            name = _local(el.tag)
            if name in ('hr', 'heartrate') and (el.text or '').strip().isdigit():
                hr = int(el.text)
            elif name in ('cad', 'cadence') and (el.text or '').strip().isdigit():
                cad = int(el.text)
            elif name in ('power', 'watts', 'PowerInWatts') and (el.text or '').strip().lstrip('-').isdigit():
                power = int(el.text)
        pts.append(RidePoint(t=t, lat=lat, lon=lon,
                             ele=float(ele) if ele else None,
                             hr=hr, cad=cad, power=power))
    ride.points = pts
    ride.fix_totals()
    return ride


def write_gpx(ride: Ride) -> bytes:
    ET.register_namespace('', 'http://www.topografix.com/GPX/1/1')
    ET.register_namespace('gpxtpx', 'http://www.garmin.com/xmlschemas/TrackPointExtension/v1')
    root = ET.Element('{http://www.topografix.com/GPX/1/1}gpx', {
        'version': '1.1', 'creator': 'meterhub'})
    meta = ET.SubElement(root, 'metadata')
    ET.SubElement(meta, 'name').text = ride.title or (ride.start.strftime('%Y-%m-%d %H:%M') if ride.start else 'ride')
    ET.SubElement(meta, 'time').text = (ride.start or datetime.now(timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ')
    trk = ET.SubElement(root, 'trk')
    ET.SubElement(trk, 'name').text = ride.title or 'ride'
    ET.SubElement(trk, 'type').text = 'cycling'
    seg = ET.SubElement(trk, 'trkseg')
    tpns = 'http://www.garmin.com/xmlschemas/TrackPointExtension/v1'
    for p in ride.points:
        el = ET.SubElement(seg, 'trkpt', {'lat': f'{p.lat:.7f}', 'lon': f'{p.lon:.7f}'})
        if p.ele is not None:
            ET.SubElement(el, 'ele').text = f'{p.ele:.1f}'
        ET.SubElement(el, 'time').text = p.t.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        if p.hr or p.cad:
            ext = ET.SubElement(el, 'extensions')
            tpx = ET.SubElement(ext, f'{{{tpns}}}TrackPointExtension')
            if p.hr:
                ET.SubElement(tpx, f'{{{tpns}}}hr').text = str(int(p.hr))
            if p.cad:
                ET.SubElement(tpx, f'{{{tpns}}}cad').text = str(int(p.cad))
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def read_tcx(path: str | Path) -> Ride:
    root = ET.parse(str(path)).getroot()
    ride = Ride(source='tcx', sid=Path(path).stem, title=Path(path).stem)
    pts: list[RidePoint] = []
    for tp in root.iter():
        if _local(tp.tag) != 'Trackpoint':
            continue
        t_raw = _text(tp, 'Time')
        try:
            t = datetime.fromisoformat(t_raw.replace('Z', '+00:00'))
        except ValueError:
            continue
        pos = _dig(tp, 'Position')
        lat = lon = None
        if pos is not None:
            try:
                lat = float(_text(pos, 'LatitudeDegrees'))
                lon = float(_text(pos, 'LongitudeDegrees'))
            except ValueError:
                lat = lon = None
        if lat is None or lon is None:
            continue
        ele = _text(tp, 'AltitudeMeters')
        hr_el = _dig(tp, 'HeartRateBpm')
        hr_raw = _text(hr_el, 'Value') if hr_el is not None else ''
        dist = _text(tp, 'DistanceMeters')
        cad = spd = pow_ = None
        for el in tp.iter():
            n = _local(el.tag)
            if n == 'Cadence' and (el.text or '').strip().isdigit():
                cad = int(el.text)
            elif n == 'Speed' and (el.text or '').strip():
                spd = float(el.text)
            elif n == 'Watts' and (el.text or '').strip().isdigit():
                pow_ = int(el.text)
        pts.append(RidePoint(
            t=t, lat=lat, lon=lon,
            ele=float(ele) if ele else None,
            hr=int(hr_raw) if hr_raw.isdigit() else None,
            cad=cad, power=pow_, speed=spd,
            dist=float(dist) if dist else None,
        ))
    ride.points = pts
    ride.fix_totals()
    return ride


def write_tcx(ride: Ride) -> bytes:
    ns = 'http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2'
    ax = 'http://www.garmin.com/xmlschemas/ActivityExtension/v2'
    ET.register_namespace('', ns)
    ET.register_namespace('ax', ax)
    root = ET.Element(f'{{{ns}}}TrainingCenterDatabase')
    acts = ET.SubElement(root, f'{{{ns}}}Activities')
    act = ET.SubElement(acts, f'{{{ns}}}Activity', {'Sport': 'Biking'})
    start = ride.start or (ride.points[0].t if ride.points else datetime.now(timezone.utc))
    ET.SubElement(act, f'{{{ns}}}Id').text = start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    lap = ET.SubElement(act, f'{{{ns}}}Lap', {'StartTime': start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})
    ET.SubElement(lap, f'{{{ns}}}TotalTimeSeconds').text = f'{ride.elapsed_s or ride.moving_s:.0f}'
    ET.SubElement(lap, f'{{{ns}}}DistanceMeters').text = f'{ride.distance_m:.1f}'
    ET.SubElement(lap, f'{{{ns}}}Calories').text = '0'
    ET.SubElement(lap, f'{{{ns}}}Intensity').text = 'Active'
    trk = ET.SubElement(lap, f'{{{ns}}}Track')
    for p in ride.points:
        tp = ET.SubElement(trk, f'{{{ns}}}Trackpoint')
        ET.SubElement(tp, f'{{{ns}}}Time').text = p.t.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        pos = ET.SubElement(tp, f'{{{ns}}}Position')
        ET.SubElement(pos, f'{{{ns}}}LatitudeDegrees').text = f'{p.lat:.7f}'
        ET.SubElement(pos, f'{{{ns}}}LongitudeDegrees').text = f'{p.lon:.7f}'
        if p.ele is not None:
            ET.SubElement(tp, f'{{{ns}}}AltitudeMeters').text = f'{p.ele:.1f}'
        if p.dist is not None:
            ET.SubElement(tp, f'{{{ns}}}DistanceMeters').text = f'{p.dist:.1f}'
        if p.hr:
            hb = ET.SubElement(tp, f'{{{ns}}}HeartRateBpm')
            ET.SubElement(hb, f'{{{ns}}}Value').text = str(int(p.hr))
        if p.cad or p.speed or p.power:
            ext = ET.SubElement(tp, f'{{{ns}}}Extensions')
            tpx = ET.SubElement(ext, f'{{{ax}}}TPX')
            if p.speed:
                ET.SubElement(tpx, f'{{{ax}}}Speed').text = f'{p.speed:.3f}'
            if p.cad:
                ET.SubElement(tpx, f'{{{ax}}}RunCadence').text = str(int(p.cad))
            if p.power:
                ET.SubElement(tpx, f'{{{ax}}}Watts').text = str(int(p.power))
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def read_any(path: str | Path) -> Ride:
    p = Path(path)
    suf = p.suffix.lower()
    if suf == '.fit':
        from .fitread import read_fit
        return read_fit(p)
    if suf == '.gpx':
        return read_gpx(p)
    if suf == '.tcx':
        return read_tcx(p)
    raise ValueError(f'不支持的格式：{p.suffix}')
