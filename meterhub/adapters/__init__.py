# -*- coding: utf-8 -*-
"""适配器注册表。新增平台只要在这里登记一行。"""
from __future__ import annotations

from .base import Adapter
from .garmin import Garmin
from .igpsport import IGPSPORT
from .localdir import LocalFiles
from .onelap import Onelap
from .strava import Strava
from .wahoo import Wahoo
from .xingzhe import Xingzhe

_INSTANCES: dict[str, Adapter] = {
    a.key: a for a in (Xingzhe(), IGPSPORT(), Onelap(), Garmin(), Strava(), Wahoo(),
                       LocalFiles())
}


def all_adapters() -> list[Adapter]:
    return list(_INSTANCES.values())


def get(key: str) -> Adapter:
    if key not in _INSTANCES:
        raise KeyError(f'未知来源：{key}')
    return _INSTANCES[key]


def by_cap(cap: str) -> list[Adapter]:
    return [a for a in _INSTANCES.values() if cap in a.caps]


__all__ = ['Adapter', 'all_adapters', 'get', 'by_cap']
