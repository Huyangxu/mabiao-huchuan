# -*- coding: utf-8 -*-
"""各平台登录。界面只调这里的函数，适配器只认 config 里的 token。

登录方式按平台**当前实际能用**的来，不硬凑：
  * 行者：App 扫码（短信通道已关，见下）
  * iGPSPORT：手机验证码（接口正常）
  * 佳明：账号+密码，中国站账号就是手机号（也可以填邮箱），开了二次验证会要验证码
  * 迈金/顽鹿：账号+密码（顽鹿网页端本来就只有密码登录）
"""
from __future__ import annotations

import re
import uuid

from . import config, garmin_auth, net
from ._vendor import segno
from .adapters.onelap import Onelap


class LoginError(RuntimeError):
    pass


# ---------------- 行者（App 扫码） ----------------
#
# 实测：行者短信通道已关。
#   POST /api/v1/mobile/send_sms/ -> HTTP 400
#   {"code":1,"data":{"sms_service_error":"发送短信失败"},"msg":"发送短信失败"}
# 只剩 App 扫码这一条路。协议是从行者 H5 的登录页 chunk 里反查出来的（166.js），
# 三步都已实测（未扫码态返回 status:1）：
#   1. POST /api/scan_login/qrcode/  {scene_type:1, device_type:1, device_sn:""}
#        -> {code:0, data:{token:"<32hex>", expires_in:120}}
#   2. 二维码内容：imxingzhe://app.imxingzhe.com/login/confirm?token=<token>
#        （行者 App 扫这个 scheme 会拉起确认页）
#   3. GET /api/scan_login/status/?token=<token>
#        data.status = 1 未扫码 / 2 已扫待确认
#        data 里出现 access_token 即为成功（前端随后把
#        "access_token&refresh_token" 写进 cookie _XingzheWeb_AssessToken，
#        取 & 前半段作 Bearer —— 与验证版一致）
#   错误码：4001 已失效 / 4002 已被使用 / 4004 手机端取消 / 4005 无效用户

XZ = 'https://www.imxingzhe.com'
XZ_HDR = {'Referer': XZ + '/login/', 'Origin': XZ,
          'Accept': 'application/json, text/plain, */*',
          'X-Requested-With': 'XMLHttpRequest',
          'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                         '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')}
XZ_SCHEME = 'imxingzhe://app.imxingzhe.com/login/confirm?token='
XZ_ERR = {4001: '二维码已失效，请刷新', 4002: '二维码已被使用，请刷新',
          4004: '手机上取消了登录', 4005: '行者账号异常，请重试'}


def qr_svg(content: str) -> str:
    """把内容画成二维码，返回可直接塞进 <img src> 的 data URI。"""
    q = segno.make(content, error='m', micro=False)
    return q.svg_data_uri(scale=4, border=2, dark='#111111', light='#ffffff')


def xz_qr_start() -> dict:
    """申请一个扫码登录二维码。

    参数按网页端扫码协议（与验证版一致）：device_sn 用 uuid 而不是空串，
    空串也能拿到 token，但服务端可能把 token 和设备绑定，扫码后容易对不上。
    """
    r = net.post(f'{XZ}/api/scan_login/qrcode/',
                 json={'scene_type': 1, 'device_type': 1,
                       'device_sn': uuid.uuid4().hex},
                 headers=XZ_HDR, timeout=30)
    try:
        j = r.json()
    except ValueError:
        raise LoginError(f'行者二维码接口返回异常（HTTP {r.status_code}）：{r.text[:120]}')
    if j.get('code') not in (0, '0'):
        raise LoginError(f"申请二维码失败：{j.get('msg') or r.text[:120]}")
    data = j.get('data') or {}
    token = data.get('token') or ''
    if not token:
        raise LoginError('行者没返回二维码 token')
    content = XZ_SCHEME + token
    return {'token': token,
            'expires_in': int(data.get('expires_in') or 120),
            'content': content,
            'svg': qr_svg(content)}


def xz_qr_poll(token: str) -> dict:
    """查一次扫码状态。state: waiting / scanned / ok / error"""
    if not token:
        raise LoginError('缺少二维码 token')
    r = net.get(f'{XZ}/api/scan_login/status/', params={'token': token},
                headers=XZ_HDR, timeout=25)
    try:
        j = r.json()
    except ValueError:
        raise LoginError(f'查询扫码状态失败（HTTP {r.status_code}）：{r.text[:120]}')

    code = j.get('code')
    data = j.get('data') or {}

    access = data.get('access_token') or data.get('token') or ''
    if isinstance(access, str) and len(access) > 20:
        config.save(xz_token=access, xz_phone='')
        return {'state': 'ok', 'message': '行者登录成功'}

    if code not in (0, '0'):
        raise LoginError(XZ_ERR.get(int(code) if str(code).isdigit() else 0,
                                    j.get('msg') or f'查询失败（{code}）'))

    st = data.get('status')
    if st == 1:
        return {'state': 'waiting', 'message': '等待手机扫码'}
    if st == 2:
        return {'state': 'scanned', 'message': '已扫码，请在手机上点确认'}
    raise LoginError(f'看不懂的扫码状态：{j}')


# ---------------- iGPSPORT（手机验证码） ----------------

IGP = {'cn': 'https://prod.zh.igpsport.com', 'intl': 'https://prod.en.igpsport.com'}


def _igp_hdr(token: str = '') -> dict:
    h = {'Accept': 'application/json', 'Accept-Language': 'zh-CN',
         'TimeZone': 'Asia/Shanghai', 'Qiwu-App-Version': '7.07.08',
         'X-Platform': 'web', 'Content-Type': 'application/json',
         'Origin': 'https://www.igpsport.com', 'Referer': 'https://www.igpsport.com/'}
    if token:
        h['Authorization'] = f'Bearer {token}'
    return h


def _igp_find_token(text: str) -> str:
    for pat in (r'"access_token"\s*:\s*"([^"]+)"', r'"token"\s*:\s*"([^"]+)"',
                r'<access_token>\s*([^<]+)'):
        m = re.search(pat, text)
        if m:
            return m.group(1).strip()
    return ''


def _igp_err(text: str, fallback: str) -> str:
    """把 iGPSPORT 的错误码翻成人话。"""
    low = (text or '').lower()
    if 'account does not exist' in low or '"code":4001' in low:
        return 'iGPSPORT 没有这个手机号，先确认号码，或去 App 里注册过再试'
    if 'code":4002' in low or 'password' in low:
        return 'iGPSPORT 账号或密码不对'
    return f'{fallback}：{text[:160]}'


def igp_send(region: str, phone: str) -> str:
    r = net.post(f'{IGP.get(region, IGP["cn"])}/service/auth/account/SendVerificationCode',
                 json={'type': 1, 'username': phone}, headers=_igp_hdr())
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code >= 300 or (j.get('code') not in (None, 0, '0', 200)):
        raise LoginError(_igp_err(r.text, '验证码发送失败'))
    return '验证码已发送，查收短信'


def igp_login(region: str, phone: str, code: str) -> str:
    base = IGP.get(region, IGP['cn'])
    r = net.post(f'{base}/service/auth/account/login/phone',
                 json={'appId': 'igpsport-web', 'phone': phone, 'code': code},
                 headers=_igp_hdr())
    token = _igp_find_token(r.text)
    if not token:
        raise LoginError(_igp_err(r.text, '登录失败'))
    for path, body in (('/service/web-gateway/web-analyze/webActivityAnalyze/active', {}),
                       ('/service/web-gateway/web-analyze/memberInfo/notifyForLogin',
                        {'loginType': 2})):
        try:
            net.post(base + path, json=body, headers=_igp_hdr(token))
        except Exception:
            pass
    config.save(igp_token=token, igp_phone=phone, igp_region=region)
    return token


# ---------------- 佳明（账号+密码，可能带验证码） ----------------

def garmin_login(account: str, password: str, region: str = 'cn') -> dict:
    """account 中国站填手机号（用户多数没设过邮箱），国际站填邮箱。"""
    try:
        tokens = garmin_auth.start_login(account, password, region)
    except garmin_auth.MFARequired as need:
        # 账号密码是对的，只差验证码。先把账号记住（密码不存，等真登录成功再存），
        # 免得用户还得多打一遍。
        config.save(garmin_email=account, garmin_region=region)
        return {'mfa': True, 'method': need.method,
                'message': f'佳明发了一个验证码到你{need.method}，请输入验证码'}
    garmin_auth.save_tokens(tokens)
    config.save(garmin_email=account, garmin_password=password, garmin_region=region)
    return {'mfa': False, 'message': f'佳明登录成功（{account}）'}


def garmin_mfa(code: str) -> dict:
    tokens = garmin_auth.continue_mfa(code)
    garmin_auth.save_tokens(tokens)
    return {'mfa': False, 'message': '佳明登录成功'}


# ---------------- 迈金 / 顽鹿（账号+密码） ----------------

def onelap_login(account: str, password: str) -> dict:
    st = Onelap.login(account, password)
    config.save(onelap_account=account, onelap_password=password,
                onelap_token=st['token'], onelap_refresh=st['refresh'],
                onelap_uid=st['uid'])
    return {'message': f"顽鹿登录成功（uid={st['uid'] or '未返回'}）"}
