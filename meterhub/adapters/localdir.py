# -*- coding: utf-8 -*-
"""本地文件适配器：从文件夹/压缩包导入，或把记录导出成文件。

这是所有「没有公开 API」的平台（Cateye、Sigma、COOSPO、部分国产码表）的通用通道：
App 导出 → 丢进这里 → 转成目标平台要的格式。
"""
from __future__ import annotations

from pathlib import Path

from .. import bundle, config
from ..model import Ride
from .base import Adapter


class LocalFiles(Adapter):
    key = 'file'
    name = '本地文件 / 导出包'
    caps = ('pull', 'push')
    note = '支持 .fit/.gpx/.tcx 单文件、整个文件夹、以及 Garmin/Strava/黑鸟等官方导出 zip。'

    def __init__(self, inbox: str | Path | None = None, outbox: str | Path | None = None):
        self.inbox = Path(inbox) if inbox else config.home() / 'inbox'
        self.outbox = Path(outbox) if outbox else config.home() / 'outbox'

    def pull(self, limit: int = 0, log=None) -> list[Ride]:
        if not self.inbox.exists():
            raise RuntimeError(f'导入目录不存在：{self.inbox}')
        rides = bundle.collect(self.inbox, log=log)
        if log:
            log(f'从 {self.inbox} 读到 {len(rides)} 条')
        return rides[:limit] if limit else rides

    def push(self, ride: Ride, log=None) -> str:
        if not ride.points:
            raise RuntimeError('没有轨迹点，导出的文件没有意义')
        self.outbox.mkdir(parents=True, exist_ok=True)
        written = bundle.export_all([ride], self.outbox, fmt='fit')
        return f'file:{written[0].name}'
