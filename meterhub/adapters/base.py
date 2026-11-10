# -*- coding: utf-8 -*-
"""适配器基类与能力声明。

每个平台只声明它真实具备的能力，界面按能力显示按钮，不假装支持：
  pull  = 能把平台上的历史记录读下来（带轨迹）
  push  = 能把轨迹上传到平台并生成一条记录
  file  = 只能靠文件交换（导出包导入 / 生成文件后手动上传）
"""
from __future__ import annotations

from ..model import Ride


class AlreadyExists(RuntimeError):
    """目标平台已经有这条记录了。

    单独一个类型，是为了让 pipeline 把它算成「跳过」而不是「失败」。
    典型场景：Garmin 上传重复 FIT 返回 409 DUPLICATE，
    这是好事（说明之前传过），不该在结果里报红。
    """


class Adapter:
    key = ''
    name = ''
    caps: tuple[str, ...] = ()
    note = ''          # 一句话限制说明，直接显示在界面上
    needs: tuple[str, ...] = ()   # 需要配置的凭据

    # ---------------- 状态 ----------------

    def ready(self) -> tuple[bool, str]:
        return True, ''

    def hint(self) -> str:
        return self.note

    # ---------------- 数据 ----------------

    def pull(self, limit: int = 0, log=None) -> list[Ride]:
        raise NotImplementedError

    def push(self, ride: Ride, log=None) -> str:
        """上传成功返回平台侧 id 或描述。"""
        raise NotImplementedError

    def existing(self, log=None) -> set[str]:
        """平台已有记录的指纹集合，用于跳过重复上传。默认不支持。"""
        return set()

    # ---------------- 描述 ----------------

    def info(self) -> dict:
        ok, why = self.ready()
        return {
            'key': self.key, 'name': self.name, 'caps': list(self.caps),
            'note': self.note, 'ready': ok, 'why': why,
            'needs': list(self.needs),
        }
