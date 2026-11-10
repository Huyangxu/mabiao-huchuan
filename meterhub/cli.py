# -*- coding: utf-8 -*-
"""命令行入口。

    python -m meterhub                     打开本地界面
    python -m meterhub state               看各平台就绪状态与本地库统计
    python -m meterhub transfer xingzhe igpsport --limit 5
    python -m meterhub transfer file igpsport --path D:\\export.zip
    python -m meterhub import D:\\garmin-export.zip
    python -m meterhub lib                 列出本地库
    python -m meterhub config xz_token=xxx
"""
from __future__ import annotations

import argparse
import sys


def _log(msg):
    print(f'  {msg}', flush=True)


def cmd_state(_a) -> int:
    from . import adapters, config, store
    c = config.masked()
    print(f"meterhub v{c['version']}   数据目录 {c['home']}")
    print(f"本地库：{store.stats()}")
    print()
    for a in adapters.all_adapters():
        ok, why = a.ready()
        mark = '就绪' if ok else f'未就绪（{why}）'
        caps = '+'.join(a.caps)
        print(f"  {a.name:<22} [{caps:<9}] {mark}")
        if a.note:
            print(f"    {a.note}")
    return 0


def cmd_transfer(a) -> int:
    from . import adapters, pipeline
    src = adapters.get(a.source)
    if a.path and hasattr(src, 'inbox'):
        src.inbox = a.path
    res = pipeline.transfer(a.source, a.target, limit=a.limit,
                            skip_existing=not a.force, dry_run=a.dry_run, log=_log)
    print()
    print(res['text'])
    if res['errors']:
        print('失败明细：')
        for e in res['errors']:
            print('  -', e)
    return 0 if res['failed'] == 0 else 1


def cmd_import(a) -> int:
    from . import pipeline
    r = pipeline.import_path(a.path, log=_log)
    print(f"\n读到 {r['read']} 条，新入库 {r['added']} 条")
    return 0


def cmd_lib(a) -> int:
    from . import store
    rows = store.listing()
    if not rows:
        print('本地库是空的')
        return 0
    for r in rows[:a.limit or len(rows)]:
        flags = f"{r.get('start','')}  {r.get('distance_km',0):>7.2f}km  {r.get('moving_min',0):>5.0f}min"
        syn = ','.join(r.get('synced_to') or []) or '-'
        print(f"  {flags}  {r.get('source',''):<9} →{syn:<12} {r.get('title','')[:34]}")
    print(f"\n共 {len(rows)} 条")
    return 0


def cmd_config(a) -> int:
    from . import config
    kw = {}
    for pair in a.pairs:
        if '=' not in pair:
            print(f'忽略（格式应为 k=v）：{pair}')
            continue
        k, v = pair.split('=', 1)
        kw[k.strip()] = v.strip()
    config.save(**kw)
    print('已保存：', ', '.join(kw))
    return 0


def cmd_serve(a) -> int:
    from . import server
    return server.main(open_browser=not a.no_browser, port=a.port)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='meterhub', description='码表数据互传枢纽')
    sub = ap.add_subparsers(dest='cmd')

    p = sub.add_parser('serve', help='打开本地界面')
    p.add_argument('--port', type=int, default=0)
    p.add_argument('--no-browser', action='store_true')
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser('state', help='平台状态与本地库统计')
    p.set_defaults(fn=cmd_state)

    p = sub.add_parser('transfer', help='从来源传到目标')
    p.add_argument('source')
    p.add_argument('target')
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--path', default='', help='来源是文件类适配器时指定路径')
    p.add_argument('--force', action='store_true', help='不跳过已同步')
    p.add_argument('--dry-run', action='store_true')
    p.set_defaults(fn=cmd_transfer)

    p = sub.add_parser('import', help='把文件/压缩包/文件夹导入本地库')
    p.add_argument('path')
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser('lib', help='列出本地库')
    p.add_argument('--limit', type=int, default=0)
    p.set_defaults(fn=cmd_lib)

    p = sub.add_parser('config', help='设置配置，如 xz_token=xxx')
    p.add_argument('pairs', nargs='+')
    p.set_defaults(fn=cmd_config)

    a = ap.parse_args(argv)
    if not getattr(a, 'cmd', None):
        ap.print_help()
        return 2
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
