# -*- coding: utf-8 -*-
"""本地 Web 界面（默认 127.0.0.1:8788）。

只用标准库，不引入 web 框架：这个工具是要能塞进 U 盘、点开就用的。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import adapters, config, login, pipeline, store

HERE = Path(__file__).resolve().parent


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # 浏览器刷新/关标签会掐断正在写的响应（WinError 10053/10054），
        # 不是程序错误，别在双击启动的黑窗口里刷一屏 traceback。
        if isinstance(sys.exc_info()[1],
                      (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Runner:
    def __init__(self):
        self.lock = threading.Lock()
        self.lines: list[dict] = []
        self.running = False
        self.result: dict | None = None
        self.error = ''
        self.direction = ''
        # 每次任务一套新的控制器，暂停/停止不会串到下一次运行
        self.control = pipeline.RunControl()

    def log(self, msg: str):
        with self.lock:
            self.lines.append({'t': time.strftime('%H:%M:%S'), 'msg': str(msg)})

    def snapshot(self, since: int) -> dict:
        with self.lock:
            new = self.lines[since:]
            return {'lines': new, 'cursor': len(self.lines), 'running': self.running,
                    'result': self.result, 'error': self.error,
                    'direction': self.direction,
                    'paused': self.running and self.control.paused,
                    'stopped': self.running and self.control.stopped}


RUN = Runner()


# ---------------- 运行记录落盘 ----------------
# 只放在内存里的话，进程一重启（改代码、重新双击启动.bat）记录就没了，
# 用户会以为“刚才跑的丢了”。所以每次运行结束都往 data\runs.jsonl **追加**一条，
# 不再互相覆盖：跑完行者→iGPSPORT 又跑佳明→iGPSPORT，前者照样能在“运行历史”里翻出来。
# last_run.json 仍然写，保持“打开页面就是上一次”的兼容行为。

def _last_run_path() -> Path:
    return config.home() / 'last_run.json'


def _runs_path() -> Path:
    return config.home() / 'runs.jsonl'


def save_run(result: dict | None, error: str, lines: list[dict],
             direction: str = '') -> None:
    if not result and not error:
        return
    payload = {
        'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'direction': direction,
        'result': result,
        'error': error,
        'lines': lines[-300:],
    }
    try:
        _last_run_path().write_text(
            json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass
    try:
        with _runs_path().open('a', encoding='utf-8') as f:
            f.write(json.dumps(payload, ensure_ascii=False) + '\n')
    except Exception:
        pass


def load_runs(limit: int = 50, with_lines: bool = False) -> list[dict]:
    """最近 limit 次运行，新的在前。默认不带日志行（列表只需要摘要）。"""
    p = _runs_path()
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            if not with_lines:
                d = {k: v for k, v in d.items() if k != 'lines'}
            out.append(d)
    except Exception:
        return out
    return out[-limit:][::-1]


def load_last_run() -> None:
    p = _last_run_path()
    if not p.exists():
        return
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return
    res = d.get('result')
    if isinstance(res, dict):
        res.setdefault('when', d.get('saved_at') or '')
        RUN.result = res
    RUN.error = d.get('error') or ''
    RUN.direction = d.get('direction') or ''
    RUN.lines = [x for x in (d.get('lines') or []) if isinstance(x, dict)]



def log_error(platform: str, exc: Exception) -> None:
    """登录/接口失败统一落盘：data\\login_error.log + 控制台。

    界面只显示一行短消息，真正定位问题要看这个文件（含完整堆栈）。
    """
    import traceback
    head = f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {platform}: {type(exc).__name__}: {exc}'
    print(head, flush=True)
    try:
        p = config.home() / 'login_error.log'
        with open(p, 'a', encoding='utf-8') as f:
            f.write(head + '\n')
            f.write(traceback.format_exc() + '\n')
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = 'meterhub/' + config.VERSION

    def log_message(self, *a):
        pass

    # ---------------- 工具 ----------------

    def _send(self, code: int, body: bytes, ctype='application/json; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    def _body(self) -> dict:
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            return {}

    # ---------------- 登录 ----------------

    def _login(self, body: dict):
        """统一登录入口。step=send 只发验证码，step=verify 校验并落 token。

        行者是个例外：短信通道已关，只能走 /api/xz/qr 扫码，这里直接拒绝。
        """
        p = (body.get('platform') or '').strip()
        step = (body.get('step') or 'verify').strip()
        region = str(body.get('region') or 'cn').strip()
        phone = str(body.get('phone') or '').strip()
        code = str(body.get('code') or '').strip()
        try:
            if p == 'xingzhe':
                return self._json({'error': '行者只能用手机 App 扫二维码登录，请点上面的「显示二维码」'}, 400)
            elif p == 'igpsport':
                if step == 'send':
                    return self._json({'ok': True, 'message': login.igp_send(region, phone)})
                login.igp_login(region, phone, code)
                msg = 'iGPSPORT 登录成功'
            elif p == 'garmin':
                r = login.garmin_login(str(body.get('account') or body.get('email') or '').strip(),
                                       str(body.get('password') or ''), region)
                return self._json({'ok': True, **r, 'config': config.masked()})
            elif p == 'onelap':
                r = login.onelap_login(str(body.get('account') or '').strip(),
                                       str(body.get('password') or ''))
                msg = r['message']
            else:
                return self._json({'error': f'未知平台：{p}'}, 400)
            return self._json({'ok': True, 'message': msg, 'config': config.masked()})
        except Exception as exc:
            log_error(f'login/{p}', exc)
            return self._json({'error': str(exc)}, 400)

    # ---------------- 路由 ----------------

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ('/', '/index.html'):
            html = (HERE / 'ui.html').read_bytes()
            return self._send(200, html, 'text/html; charset=utf-8')
        if u.path == '/api/state':
            return self._json({
                'config': config.masked(),
                'adapters': [a.info() for a in adapters.all_adapters()],
                'stats': store.stats(),
            })
        if u.path == '/api/log':
            since = int((parse_qs(u.query).get('since') or ['0'])[0])
            return self._json(RUN.snapshot(since))
        if u.path == '/api/runs':
            # 运行历史：默认只给摘要，?idx=序号 再取那一次的完整日志
            q = parse_qs(u.query)
            idx = (q.get('idx') or [''])[0]
            if idx != '':
                rows = load_runs(limit=500, with_lines=True)
                try:
                    i = int(idx)
                except Exception:
                    return self._json({'error': 'idx 不是数字'}, 400)
                if i < 0 or i >= len(rows):
                    return self._json({'error': '没有这条记录'}, 404)
                r = rows[i]
                r['idx'] = i
                return self._json(r)
            n = int((q.get('limit') or ['50'])[0] or 50)
            rows = load_runs(limit=max(1, min(n, 500)))
            for i, r in enumerate(rows):
                r['idx'] = i
            return self._json({'rows': rows})
        if u.path == '/api/library':
            n = int((parse_qs(u.query).get('limit') or ['200'])[0])
            return self._json({'rows': store.listing()[:n], 'stats': store.stats()})
        return self._json({'error': 'not found'}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()

        if u.path == '/api/config':
            kw = {k: v for k, v in body.items() if k in config.DEFAULTS}
            config.save(**kw)
            return self._json({'ok': True, 'config': config.masked()})

        if u.path == '/api/transfer':
            if RUN.running:
                return self._json({'error': '已有任务在跑，请等它结束'}, 409)
            source, target = body.get('source', ''), body.get('target', '')
            RUN.__init__()
            RUN.running = True
            RUN.direction = f'{source} → {target}'
            control = RUN.control
            RUN.log(f"开始：{source} → {target}")

            def work():
                try:
                    res = pipeline.transfer(
                        source, target,
                        limit=int(body.get('limit') or 0),
                        skip_existing=bool(body.get('skip', True)),
                        dry_run=bool(body.get('dry_run')),
                        log=RUN.log, control=control)
                    RUN.result = res
                except pipeline.StopRequested:
                    RUN.result = {'text': '已停止（用户中止）', 'stopped': True,
                                  'pulled': 0, 'new': 0, 'pushed': 0,
                                  'skipped': 0, 'failed': 0, 'errors': []}
                except Exception as exc:
                    RUN.error = str(exc)
                    RUN.log(f'中止：{exc}')
                finally:
                    RUN.running = False
                    if RUN.result:
                        RUN.result['when'] = time.strftime('%m-%d %H:%M')
                    save_run(RUN.result, RUN.error, RUN.lines, RUN.direction)

            threading.Thread(target=work, daemon=True).start()
            return self._json({'ok': True})

        if u.path == '/api/login':
            return self._login(body)

        if u.path == '/api/xz/qr':
            try:
                return self._json({'ok': True, **login.xz_qr_start()})
            except Exception as exc:
                log_error('xz/qr', exc)
                return self._json({'error': str(exc)}, 400)

        if u.path == '/api/xz/qr/poll':
            try:
                r = login.xz_qr_poll(str(body.get('token') or '').strip())
                return self._json({'ok': True, **r, 'config': config.masked()})
            except Exception as exc:
                log_error('xz/qr/poll', exc)
                return self._json({'error': str(exc)}, 400)

        if u.path == '/api/mfa':
            try:
                r = login.garmin_mfa(str(body.get('code') or ''))
                return self._json({'ok': True, **r, 'config': config.masked()})
            except Exception as exc:
                log_error('mfa/garmin', exc)
                return self._json({'error': str(exc)}, 400)

        if u.path == '/api/import':
            path = (body.get('path') or '').strip()
            if not path:
                return self._json({'error': '请填路径'}, 400)
            if RUN.running:
                return self._json({'error': '已有任务在跑'}, 409)
            RUN.__init__()
            RUN.running = True
            RUN.direction = '手动导入'
            control = RUN.control
            RUN.log(f'导入：{path}')

            def work():
                try:
                    RUN.result = pipeline.import_path(path, log=RUN.log, control=control)
                except pipeline.StopRequested:
                    RUN.result = {'text': '已停止（用户中止）', 'stopped': True}
                except Exception as exc:
                    RUN.error = str(exc)
                    RUN.log(f'中止：{exc}')
                finally:
                    RUN.running = False
                    if RUN.result:
                        RUN.result['when'] = time.strftime('%m-%d %H:%M')
                    save_run(RUN.result, RUN.error, RUN.lines, RUN.direction)

            threading.Thread(target=work, daemon=True).start()
            return self._json({'ok': True})

        if u.path == '/api/control':
            # 暂停 / 继续 / 停止。只在任务还在跑时有效，避免误点影响下一次运行。
            if not RUN.running:
                return self._json({'error': '当前没有在跑的任务'}, 409)
            act = (body.get('action') or '').strip().lower()
            if act == 'pause':
                RUN.control.pause()
                RUN.log('⏸ 已暂停：当前这条处理完就停住，点「继续」接着跑')
            elif act == 'resume':
                RUN.control.resume()
                RUN.log('▶ 继续运行')
            elif act == 'stop':
                RUN.control.stop()
                RUN.log('■ 正在停止…（当前这条处理完就结束，不会中断上传中的文件）')
            else:
                return self._json({'error': 'action 只能是 pause / resume / stop'}, 400)
            return self._json({'ok': True, 'paused': RUN.control.paused,
                               'stopped': RUN.control.stopped})

        return self._json({'error': 'not found'}, 404)


def main(open_browser: bool = True, port: int = 0) -> int:
    port = port or int(config.load().get('port') or 8788)
    load_last_run()          # 上次跑的结果先捞回来，界面刷新/重启都不会“一片空白”
    srv = _Server(('127.0.0.1', port), Handler)
    url = f'http://127.0.0.1:{port}/'
    print(f'meterhub v{config.VERSION}  数据目录：{config.home()}')
    print(f'界面：{url}   （Ctrl+C 退出）')
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已退出')
    finally:
        srv.server_close()
    return 0
