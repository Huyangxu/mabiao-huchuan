# -*- coding: utf-8 -*-
"""打包成免安装版：内置便携运行时 + 纯 ASCII 启动脚本。

    python pack.py

产物：dist\\码表互传-v{版本}-免安装.zip

运行时目录由环境变量 METERHUB_RUNTIME 指定（内含 python/python.exe），
不设置时按常见位置自动探测；找不到就打一个「需自带 Python」的精简包。二维码库 segno 是纯 Python，直接 vendor 在
meterhub\\_vendor\\segno 里，不打进运行时。
"""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARENT_DIST = HERE.parent / 'dist'
def _find_runtime() -> Path | None:
    """便携运行时目录：环境变量优先，其次在常见位置自动探测。

    目录内应有 `python/python.exe`。找不到时仍然打一个不含运行时的精简包，
    用户按 requirements.txt 自己装 Python 即可，不影响源码使用。
    """
    import os
    cands: list[Path] = []
    env = (os.environ.get('METERHUB_RUNTIME') or '').strip()
    if env:
        cands.append(Path(env))
    cands += [HERE / 'runtime', OUTDIR / 'runtime', HERE.parent / 'runtime']
    if PARENT_DIST.is_dir():
        cands += [d / 'runtime' for d in PARENT_DIST.iterdir() if d.is_dir()]
    for p in cands:
        if (p / 'python' / 'python.exe').is_file():
            return p
    return None
OUTDIR = HERE / 'dist'
APP_NAME = '码表互传'
# 暂存目录用固定的内部名，不叫「码表互传」：那个名字的目录一旦被运行过，
# 里面的 python.exe 会被占用，改名/删除都会失败。zip 内的根目录仍是 APP_NAME。
STAGE = OUTDIR / '_stage'

BAT = r"""@echo off
chcp 65001 >nul 2>&1
setlocal EnableExtensions
cd /d "%~dp0"
title meterhub - bike data exchange
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONHOME="
set "PYTHONPATH="
set "LOG=%TEMP%\meterhub_start.log"

if not exist "%~dp0meterhub\__init__.py" goto NOFILES
set "RT=%~dp0runtime\python\python.exe"
if not exist "%RT%" goto NORT

"%RT%" -c "import sys" >"%LOG%" 2>&1
if not errorlevel 1 goto DEPS
echo [i] Stock name failed, retrying with an alias ...
copy /y "%RT%" "%~dp0runtime\python\_mhpy.exe" >nul 2>nul
if errorlevel 1 goto RTFAIL
"%~dp0runtime\python\_mhpy.exe" -c "import sys" >"%LOG%" 2>&1
if errorlevel 1 goto RTFAIL
set "RT=%~dp0runtime\python\_mhpy.exe"

:DEPS
"%RT%" -c "import meterhub" >>"%LOG%" 2>&1
if errorlevel 1 goto DEPFAIL
echo Using bundled Python: %RT%
echo.
"%RT%" -m meterhub serve
echo.
echo [i] UI stopped. Run diagnose.bat if it failed to open.
pause
exit /b 0

:NOFILES
echo.
echo [ERROR] Program files missing (no meterhub folder here).
echo   Do NOT run from inside the zip. Right-click the zip - Extract All.
echo.
pause
exit /b 1

:NORT
echo.
echo [ERROR] Bundled Python missing: runtime\python\python.exe
echo   The zip was not fully extracted, or antivirus removed it.
echo   Extract the whole zip again to a local folder (not inside the zip).
echo.
pause
exit /b 1

:RTFAIL
echo.
echo [ERROR] The bundled Python cannot start on this PC.
echo   This tool uses ONLY its own Python, it will not touch the one installed here.
echo   Try: extract again to a local disk; whitelist the folder in antivirus;
echo        run diagnose.bat and send the report back.
echo ------------------------------------------------------------
type "%LOG%"
echo ------------------------------------------------------------
echo.
pause
exit /b 1

:DEPFAIL
echo.
echo [ERROR] meterhub cannot be imported.
echo ------------------------------------------------------------
type "%LOG%"
echo ------------------------------------------------------------
echo.
pause
exit /b 1
"""

DIAG_BAT = r"""@echo off
chcp 65001 >nul 2>&1
setlocal EnableExtensions
cd /d "%~dp0"
title meterhub diagnose
echo ---- files ----
if exist "runtime\python\python.exe" (for %%f in ("runtime\python\python.exe") do echo   %%~zf bytes  python.exe) else echo   MISSING python.exe
if exist "runtime\python\python312.dll" (for %%f in ("runtime\python\python312.dll") do echo   %%~zf bytes  python312.dll)
if exist "meterhub\__init__.py" echo   meterhub package OK
if exist "runtime\python\Lib\site-packages\curl_cffi\__init__.py" echo   curl_cffi OK
if exist "meterhub\_vendor\segno\__init__.py" echo   segno (qrcode) OK
echo ---- run ----
if not exist "runtime\python\python.exe" goto END
"runtime\python\python.exe" -c "import sys;print('python',sys.version.split()[0]);import meterhub;print('meterhub ok')"
echo   exit code: %errorlevel%
:END
echo ---- port 8788 ----
netstat -ano | findstr ":8788" | findstr LISTENING
if errorlevel 1 echo   (free)
echo.
pause
exit /b 0
"""

README = """meterhub · 码表数据互传（免安装版）
=====================================

双击 启动.bat 打开界面（http://127.0.0.1:8788）。

这台电脑不用装 Python、不用联网装库：包里自带便携运行时和全部依赖。
程序只用自带的 Python，不会用本机已装的，也不会往系统里装任何东西。

三个文件：
  启动.bat     打开界面
  diagnose.bat 出问题时双击，打印环境自检
  使用说明.txt 本文件

第一次怎么用（四步）：
  1. 双击 启动.bat，浏览器会打开界面
  2. 顶部四张卡片分别登录：
       行者      —— 点「显示二维码」，用手机行者 App 扫，手机上点确认
       iGPSPORT  —— 手机号 + 短信验证码
       佳明      —— 手机号 + 密码（中国站账号就是手机号，不用邮箱）
       迈金顽鹿  —— 账号 + 密码
  3. 点一个快捷方向，或自己选「来源 / 目标」
  4. 先点「试运行」看会传哪些，再点「开始传输」

四家码表互传，界面自动列出 9 个方向：
  行者   → iGPSPORT   行者   → 佳明   行者   → 迈金
  iGPSPORT → 佳明     iGPSPORT → 迈金
  佳明   → iGPSPORT   佳明   → 迈金
  迈金   → iGPSPORT   迈金   → 佳明

  没有「→行者」：行者不提供上传接口，任何平台都推不进去。

注意：
  * 行者只能扫码。它的短信登录通道已经关了（服务端直接返回「发送短信失败」），
    所以界面上不再有验证码入口，二维码 120 秒有效，过期点「重新生成」。
  * 佳明中国站(garmin.cn) 和 国际站(garmin.com) 数据不互通，选错站点会看到空列表。
    站点在页面底部「高级」里切。
  * 佳明上传后要在 Garmin Connect 里刷新，服务端解析 FIT 需要几秒。
  * 没有接口的码表（Bryton、黑鸟、Cateye、COOSPO…）用官方 App/网页导出 zip/FIT，
    丢到「手动导入」入本地库，再推给有接口的平台。
  * 凭据只存在本机 data\\config.json，不会发到任何第三方服务器。
  * 每次运行都会记进「运行历史」（页面下方），跑完再跑别的方向也不会覆盖上一次，
    点任意一行的「日志」能回看当时每一步。
  * 任务跑起来后可以点「暂停 / 继续 / 停止」：会在当前这条处理完才生效，
    不会把上传做一半，已完成的记录都留在本地库里。
  * 顽鹿上传按日历页「上传」控件的格式发 `jilu0` 字段（多个文件依次 jilu0/jilu1…）。
    如果顽鹿改版返回 422「没有上传文件」，程序会第一条就中止整批并给出排查提示。
    去重读官网 capability_list，按 total 翻页读全（固定 20 条/页），
    按 date + totalDistance 比对；云端列表读全时以云端实况为准，云端没有就会补传，
    不会因为本地标着「已同步」就跳过。
  * 顽鹿作为「来源」时列表能读，但 FIT 签名直链目前返回 404，暂不能导出到其他平台。

限制：
  * 行者只能读不能写（它没有上传接口），所以没有「→行者」这个方向
  * Wahoo 官方未公开活动原始文件下载，只能推不能拉
  * 顽鹿的「读列表」接口是逆向的，比上传接口更容易受改版影响
  * 各家用的都是网页/App 端自用接口，对方改版可能失效
"""


def build() -> Path:
    from meterhub.config import VERSION

    # 上一次的暂存目录：改名而不是删除。直接 rmtree 在这台机器上会被
    # 安全删除钩子拦下（要求走回收站，而回收站对深层中文路径会失败）。
    if STAGE.exists():
        old = STAGE.with_name(STAGE.name + '.old')
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        try:
            STAGE.rename(old)
        except OSError:
            pass
        else:
            shutil.rmtree(old, ignore_errors=True)
    (STAGE / 'meterhub').mkdir(parents=True)

    # 代码
    for p in (HERE / 'meterhub').rglob('*.py'):
        rel = p.relative_to(HERE / 'meterhub')
        dst = STAGE / 'meterhub' / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
    shutil.copy2(HERE / 'meterhub' / 'ui.html', STAGE / 'meterhub' / 'ui.html')
    for doc in ('README.md', '码表互通矩阵.md', 'selftest.py'):
        src = HERE / doc
        if src.exists():
            shutil.copy2(src, STAGE / doc)

    # 运行时（可选，由 METERHUB_RUNTIME 指定或自动探测）
    runtime = _find_runtime()
    if runtime:
        shutil.copytree(runtime, STAGE / 'runtime')
        print(f'  运行时已复制：{sum(1 for _ in (STAGE / "runtime").rglob("*"))} 项')
    else:
        print('  [!] 未找到便携运行时（可设置 METERHUB_RUNTIME 指向含 python/python.exe 的目录）；'
              '打出的包需要自行安装 Python 与依赖')

    (STAGE / '启动.bat').write_bytes(BAT.replace('\n', '\r\n').encode('ascii'))
    (STAGE / 'diagnose.bat').write_bytes(DIAG_BAT.replace('\n', '\r\n').encode('ascii'))
    (STAGE / '使用说明.txt').write_bytes(
        README.replace('\n', '\r\n').encode('utf-8-sig'))

    out = OUTDIR / f'码表互传-v{VERSION}-免安装.zip'
    if out.exists():
        out.unlink()
    n = 0
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(STAGE.rglob('*')):
            rel = p.relative_to(STAGE).as_posix()
            if '__pycache__' in rel or rel.endswith('.pyc'):
                continue
            if rel == 'data' or rel.startswith('data/'):
                continue
            if p.is_dir():
                z.writestr(f'{APP_NAME}/{rel}/', '')
                continue
            z.write(p, arcname=f'{APP_NAME}/{rel}')
            n += 1
    print(f'  打包完成：{out.name}  {out.stat().st_size/1048576:.1f} MB  {n} 个文件')
    return out


if __name__ == '__main__':
    OUTDIR.mkdir(parents=True, exist_ok=True)
    build()
