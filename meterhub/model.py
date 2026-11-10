# -*- coding: utf-8 -*-
"""通用骑行模型：所有平台的数据都先归一化成 Ride，再谈互传。

设计取舍：
  * 只保留骑行真正需要的字段（时间/坐标/海拔/心率/踏频/功率/速度/累计里程），
    其它平台私有指标（如 Garmin 的 VO2max）在转换时必然丢失，不如不建模。
  * 时间一律用带时区的 datetime（UTC），输出到各平台时再按需本地化。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))


@dataclass
class RidePoint:
    t: datetime
    lat: float
    lon: float
    ele: float | None = None
    hr: int | None = None
    cad: int | None = None
    power: int | None = None
    speed: float | None = None   # m/s
    dist: float | None = None    # 累计米


@dataclass
class Ride:
    source: str = ""             # 来源适配器 key
    sid: str = ""                # 平台内唯一 id
    title: str = ""
    start: datetime | None = None
    points: list[RidePoint] = field(default_factory=list)
    distance_m: float = 0.0
    moving_s: float = 0.0        # 骑行时长（去停顿）
    elapsed_s: float = 0.0       # 总跨度（含停顿）
    ascent_m: float | None = None
    sport: str = "cycling"
    raw_meta: dict = field(default_factory=dict)

    # ---------------- 派生信息 ----------------

    @property
    def end(self) -> datetime | None:
        if not self.points:
            return self.start
        return self.points[-1].t

    @property
    def has_gps(self) -> bool:
        return bool(self.points) and any(p.lat or p.lon for p in self.points)

    def fix_totals(self) -> "Ride":
        """用轨迹点回填里程/时长，避免各平台字段口径不一致。"""
        if self.points:
            if self.start is None:
                self.start = self.points[0].t
            if not self.distance_m:
                d = self.points[-1].dist
                if d:
                    self.distance_m = d
                else:
                    from .fitlib import haversine
                    self.distance_m = sum(
                        haversine((self.points[i].lat, self.points[i].lon),
                                  (self.points[i + 1].lat, self.points[i + 1].lon))
                        for i in range(len(self.points) - 1)
                        if self.points[i].lat or self.points[i].lon
                    )
            if not self.moving_s:
                self.moving_s = max(0.0, (self.points[-1].t - self.points[0].t).total_seconds())
            if not self.elapsed_s:
                # 总跨度就是首末点的真实间隔（含停顿），不能拿 moving_s 顶替，
                # 否则界面上「总时长」会跟「骑行时长」一样，和 FIT 里写的 elapsed 也不一致。
                self.elapsed_s = max(0.0, (self.points[-1].t - self.points[0].t).total_seconds())
        return self

    @property
    def fingerprint(self) -> str:
        """跨平台去重指纹：开始时间(分钟) + 里程(取整到 10m) + 是否有轨迹。

        不用文件名，因为各平台重命名规则不同；也不要求秒级一致，
        因为平台导出/解析时常把秒抹掉或四舍五入。
        """
        t = self.start.astimezone(timezone.utc).strftime('%Y%m%d%H%M') if self.start else '000000000000'
        km10 = int(round(self.distance_m / 10.0))
        return hashlib.sha1(f'{t}|{km10}|{1 if self.has_gps else 0}'.encode()).hexdigest()[:16]

    def summary(self) -> dict:
        return {
            'fingerprint': self.fingerprint,
            'source': self.source,
            'sid': self.sid,
            'title': self.title,
            'start': self.start.astimezone(BJ).strftime('%Y-%m-%d %H:%M') if self.start else '',
            'start_utc': self.start.isoformat() if self.start else '',
            'distance_km': round(self.distance_m / 1000, 2),
            'moving_min': round(self.moving_s / 60, 1),
            'elapsed_min': round(self.elapsed_s / 60, 1),
            'points': len(self.points),
            'has_gps': self.has_gps,
            'has_hr': any(p.hr for p in self.points),
            'has_power': any(p.power for p in self.points),
        }

    def describe(self) -> str:
        s = self.summary()
        flags = ''.join(['G' if s['has_gps'] else '', 'H' if s['has_hr'] else '', 'P' if s['has_power'] else ''])
        return f"{s['start']}  {s['distance_km']:>7.2f}km  {s['moving_min']:>5.0f}min  {flags:<3} {s['title'][:30]}"
