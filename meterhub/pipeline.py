# -*- coding: utf-8 -*-
"""传输编排：来源 → 本地库 → 目标。

设计原则：
  * 先落本地库再推送。这样中途失败可以重跑，也不会因为平台接口抽风丢数据。
  * 去重三路：目标平台的现有记录、本地库里"已同步到该目标"的标记、以及
    本次运行内的指纹集合。任何一路命中就跳过，避免重复上传。
  * 目标不支持 push 时直接报错，不偷偷降级成"只导出文件"——用户得知道发生了什么。
"""
from __future__ import annotations

import threading
from datetime import datetime

from . import adapters, store
from .adapters.base import AlreadyExists
from .adapters.onelap import OnelapRejected
from .model import Ride


class StopRequested(BaseException):
    """用户点了「停止」。

    故意继承 BaseException：适配器里到处是 `except Exception`，用普通 Exception
    会被某条记录的容错逻辑悄悄吞掉，停止就永远不会生效。
    """


class RunControl:
    """协作式暂停 / 停止。

    不杀线程、不中断正在进行的 HTTP 请求：只在每条记录的边界（日志回调处）
    检查一次，最多多跑当前这一条，不会把上传做一半。
    """

    def __init__(self):
        self._go = threading.Event()
        self._go.set()
        self._stop = threading.Event()

    def pause(self):
        self._go.clear()

    def resume(self):
        self._go.set()

    def stop(self):
        self._stop.set()
        self._go.set()          # 唤醒正在暂停等待的线程

    @property
    def paused(self) -> bool:
        return not self._go.is_set() and not self._stop.is_set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def checkpoint(self):
        if self._stop.is_set():
            raise StopRequested()
        self._go.wait()          # 暂停时阻塞在这里
        if self._stop.is_set():
            raise StopRequested()


class Report:
    def __init__(self):
        self.pulled = 0
        self.new = 0
        self.pushed = 0
        self.skipped = 0
        self.failed = 0
        self.errors: list[str] = []
        self.started = datetime.now()

    def as_dict(self) -> dict:
        return {
            'pulled': self.pulled, 'new': self.new, 'pushed': self.pushed,
            'skipped': self.skipped, 'failed': self.failed,
            'errors': self.errors[:20],
            'seconds': round((datetime.now() - self.started).total_seconds(), 1),
        }

    def text(self) -> str:
        d = self.as_dict()
        return (f"拉到 {d['pulled']} 条，新入库 {d['new']} 条，"
                f"推送成功 {d['pushed']} 条，跳过 {d['skipped']} 条，"
                f"失败 {d['failed']} 条，耗时 {d['seconds']}s")


def _token_of(ride: Ride, target_key: str) -> str:
    """目标平台自己的"这条记录叫什么"。平台提供 token_key 就用它，否则用指纹。"""
    fn = getattr(adapters.get(target_key), 'token_key', None)
    return fn(ride) if fn else ride.fingerprint


def transfer(source: str, target: str, limit: int = 0, skip_existing: bool = True,
             dry_run: bool = False, log=None, control: RunControl | None = None) -> dict:
    def RAW(msg):
        if log:
            log(msg)

    def L(msg):
        # 适配器拿到的是这个包装过的 log：它每打一条进度就过一次检查点，
        # 所以「读取列表」「下载 FIT」这种长阶段也能暂停/停止，不必等整个拉取跑完。
        if control:
            control.checkpoint()
        RAW(msg)

    src, tgt = adapters.get(source), adapters.get(target)
    if src.key == tgt.key:
        raise RuntimeError('来源和目标不能是同一个平台')
    if 'pull' not in src.caps:
        raise RuntimeError(f'{src.name} 不支持作为来源')
    if not dry_run and 'push' not in tgt.caps:
        raise RuntimeError(f'{tgt.name} 不支持作为目标')

    ok, why = src.ready()
    if not ok:
        raise RuntimeError(f'{src.name} 未就绪：{why}')
    if not dry_run:
        ok, why = tgt.ready()
        if not ok:
            raise RuntimeError(f'{tgt.name} 未就绪：{why}')
    else:
        ok, why = tgt.ready()
        if not ok:
            L(f'（目标{tgt.name}未就绪：{why}；试运行无法比对目标已有记录）')

    rep = Report()
    L(f'==== {src.name} → {tgt.name} ====')

    rides: list[Ride] = []
    existing: set[str] = set()
    cloud_trusted = False
    stopped = False
    aborted = ''
    try:
        # 试运行也要查目标已有记录：只读操作，不写任何东西，
        # 但少了它「试运行」会报一堆实际会被跳过的东西，清单就是假的。
        # 放在 try 里：这个阶段也可能很慢，用户点停止要能立刻停住并保留部分进度。
        if skip_existing and 'push' in tgt.caps:
            try:
                existing = tgt.existing(log=L) or set()
                # 目标端明确说「我把云端列表读全了」时，去重只认云端实况。
                # 本地 synced_to 只是「曾经推过」的历史标记：记录可能被删、
                # 上传返回 200 但云端没入库，这时仍判跳过就永远补不回来。
                cloud_trusted = bool(getattr(tgt, 'cloud_complete', False))
                if cloud_trusted:
                    L(f'以 {tgt.name} 云端实际记录为准（{len(existing)} 个去重键）')
            except Exception as exc:
                L(f'（读取目标已有记录失败，改为只按本地库去重：{exc}）')

        L(f'从 {src.name} 读取记录…')
        rides = src.pull(limit=limit, log=L)
        rep.pulled = len(rides)
        L(f'读到 {rep.pulled} 条，开始入库/推送')

        index = store.load_index()
        accepted: list[Ride] = []
        for i, ride in enumerate(rides, 1):
            tag = f'[{i}/{len(rides)}]'
            if any(store.similar(ride, s) for s in accepted):
                rep.skipped += 1
                L(f'{tag} 本次运行内同一次骑行，跳过 {ride.start}')
                continue
            accepted.append(ride)
            # 库里已有同一次骑行时，沿用它的指纹，避免同一次骑行被当成两条
            fp = store.find_similar(ride) or ride.fingerprint

            if cloud_trusted:
                # 云端列表读全了：本地标记不再单独决定跳过，云端没有就补传
                already = (fp in existing or _token_of(ride, target) in existing)
            else:
                already = (fp in index and target in (index[fp].get('synced_to') or []))
                if not already and (fp in existing or _token_of(ride, target) in existing):
                    already = True
            if already and skip_existing:
                rep.skipped += 1
                # 目标平台确实已有这条：把 synced_to 补齐（比如以前用别的工具推过），
                # 否则界面「已同步」一列永远只显示最早推过的那个平台。
                if not dry_run:
                    store.mark_synced(fp, target)
                L(f'{tag} 目标已有，跳过 {ride.start}')
                continue

            new_fp = fp
            try:
                if dry_run:
                    # 试运行只报告计划，绝不写库、不碰接口
                    rep.pushed += 1
                    L(f'{tag} [试运行] 将推送 {ride.describe()}')
                    continue
                new_fp, is_new = store.add(ride, log=L)
                if is_new:
                    rep.new += 1
                res = tgt.push(ride, log=L)
                rep.pushed += 1
                store.mark_synced(new_fp, target)
                L(f'{tag} 推送成功 {ride.describe()} → {res}')
            except AlreadyExists as exc:
                # 目标平台已有这条（佳明对重复 FIT 回 409 DUPLICATE）。
                # 这不是失败：目标端确实有数据了，直接标成已同步。
                rep.skipped += 1
                store.mark_synced(new_fp, target)
                L(f'{tag} 跳过：{exc} {ride.start}')
            except OnelapRejected as exc:
                # 接口明确拒绝（多半是字段名/接口改版），不是这条记录的问题，
                # 继续重试 200 次只是浪费时间
                rep.failed += 1
                rep.errors.append(str(exc))
                RAW(f'{tag} 顽鹿接口拒绝上传，中止整批（已推送 {rep.pushed} 条）')
                raise
            except Exception as exc:
                rep.failed += 1
                rep.errors.append(f'{ride.start}: {exc}')
                L(f'{tag} 失败 {ride.start}: {exc}')
    except OnelapRejected as exc:
        aborted = str(exc)
        RAW(f'■ 已中止：{aborted}')
        RAW('■ 这类拒绝通常与记录内容无关（接口/字段名改版），整批提前结束避免重复刷屏。')
    except StopRequested:
        stopped = True
        RAW('■ 已停止：你点了「停止」。已经推上去的记录不会重复上传，下次直接接着跑就行。')

    RAW(f'==== {"已中止" if aborted else ("已停止" if stopped else "完成")}：{rep.text()} ====')
    out = rep.as_dict()
    out['text'] = rep.text() + ('（顽鹿接口拒绝，已中止）' if aborted
                                else '（用户停止）' if stopped else '')
    out['stopped'] = stopped
    if aborted:
        out['aborted'] = True
        out['errors'] = [aborted] + rep.errors[:9]
    return out


def import_path(path: str, log=None, control: RunControl | None = None) -> dict:
    """把文件夹/压缩包/单文件导入本地库（不推送）。"""
    from . import bundle

    def RAW(m):
        if log:
            log(m)

    def L(m):
        if control:
            control.checkpoint()
        RAW(m)

    L(f'扫描 {path} …')
    rides, added, stopped = [], 0, False
    try:
        rides = bundle.collect(path, log=L)
        for i, r in enumerate(rides, 1):
            try:
                _, is_new = store.add(r, log=L)
                added += 1 if is_new else 0
                L(f'[{i}/{len(rides)}] {"新增" if is_new else "已存在"} {r.describe()}')
            except Exception as exc:
                L(f'[{i}/{len(rides)}] 失败 {exc}')
    except StopRequested:
        stopped = True
        RAW('■ 已停止：你点了「停止」，已导入的文件都保留在本地库里。')
    text = f'读到 {len(rides)} 个文件，新增 {added} 条' + ('（用户停止）' if stopped else '')
    return {'read': len(rides), 'added': added, 'text': text, 'stopped': stopped}
