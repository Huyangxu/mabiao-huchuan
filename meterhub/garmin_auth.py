# -*- coding: utf-8 -*-
"""Garmin Connect 登录（不依赖 garth）。

为什么自己实现：
  * 官方 Connect API 需要走开发者计划，个人拿 key 也要审批，且**没有写活动的接口**；
  * 网页端 SSO 已经改成 SPA + 滑块验证码，抓表单那条老路彻底走不通（实测，
    sso.garmin.cn/sso/embed 返回的是纯前端壳，页面里没有 form/_csrf）；
  * 手机 App 用的移动端 SSO 接口仍然开放，且能把 MFA 一起走完。这是目前唯一
    稳定、可自动化、还带 MFA 的登录方式。

整条链路（每一步都实测过，最后一步需要用户自己的账号）：
  1. GET  https://thegarth.s3.amazonaws.com/oauth_consumer.json   拿 OAuth1 消费方密钥
  2. GET  https://sso.{domain}/mobile/sso/en/sign-in?clientId=... 先种 cookie
  3. POST https://sso.{domain}/mobile/api/login                    账号密码
       -> responseStatus.type == SUCCESSFUL        : serviceTicketId
       -> responseStatus.type == MFA_REQUIRED      : 需要验证码，走 4
  4. POST https://sso.{domain}/mobile/api/mfa/verifyCode           短信/邮箱验证码
  5. GET  https://connectapi.{domain}/oauth-service/oauth/preauthorized?ticket=...
       -> oauth_token / oauth_token_secret（OAuth1，HMAC-SHA1 签名）
  6. POST https://connectapi.{domain}/oauth-service/oauth/exchange/user/2.0
       -> access_token / refresh_token（OAuth2，之后所有 API 用 Bearer）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import quote, urlencode

from . import config, net

CLIENT_ID = 'GCM_ANDROID_DARK'
CONSUMER_URL = 'https://thegarth.s3.amazonaws.com/oauth_consumer.json'
APP_UA = {'User-Agent': 'com.garmin.android.apps.connectmobile'}
API_UA = {'User-Agent': 'GCM-iOS-5.22.1.4'}
SSO_UA = {
    'User-Agent': ('Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) '
                   'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148'),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Dest': 'document',
}

DOMAINS = {'cn': 'garmin.cn', 'intl': 'garmin.com'}


class GarminError(RuntimeError):
    pass


def _mask(s: str) -> str:
    s = str(s or '')
    return (s[:3] + '***' + s[-2:]) if len(s) > 5 else '***'


def trace(msg: str) -> None:
    """把佳明登录每一步写进 data/garmin_trace.log（也给控制台）。

    佳明这条链路跨三个域名、两次 OAuth 换票，出错时只看一行报错没法定位，
    所以每一步都留痕。文件是追加的，一次失败就是一段完整证据。
    """
    line = f'[{time.strftime("%m-%d %H:%M:%S")}] {msg}'
    print(line, flush=True)
    try:
        with open(config.home() / 'garmin_trace.log', 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


# ---------------- OAuth1 签名（HMAC-SHA1，标准库手写） ----------------

_enc = lambda s: quote(str(s), safe='-._~')  # noqa: E731

_CONSUMER: dict[str, str] = {}


def consumer() -> dict[str, str]:
    """OAuth1 消费方密钥。这个是 App 内置常量，取一次缓存在内存就够。"""
    if _CONSUMER:
        return _CONSUMER
    r = net.get(CONSUMER_URL)
    if r.status_code != 200:
        raise GarminError(f'取 OAuth 消费方密钥失败 HTTP {r.status_code}')
    _CONSUMER.update(r.json())
    return _CONSUMER


def signature(method: str, url: str, params: dict,
              consumer_secret: str, token_secret: str = '') -> str:
    """纯函数：算 OAuth1 HMAC-SHA1 签名。单独拆出来是为了能用 RFC 向量验证。"""
    pairs = sorted((_enc(k), _enc(v)) for k, v in params.items() if v is not None)
    base = '&'.join([method.upper(), _enc(url.split('?')[0]),
                     _enc('&'.join(f'{k}={v}' for k, v in pairs))])
    key = f'{_enc(consumer_secret)}&{_enc(token_secret)}'
    return base64.b64encode(
        hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()


def sign(method: str, url: str, extra: dict | None = None,
         token: str = '', token_secret: str = '') -> str:
    """返回 OAuth1 Authorization 头。extra 是同时参与签名的 query/body 参数。"""
    c = consumer()
    oauth = {
        'oauth_consumer_key': c['consumer_key'],
        'oauth_nonce': secrets.token_hex(16),
        'oauth_signature_method': 'HMAC-SHA1',
        'oauth_timestamp': str(int(time.time())),
        'oauth_version': '1.0',
    }
    if token:
        oauth['oauth_token'] = token

    allp = dict(oauth)
    allp.update(extra or {})
    oauth['oauth_signature'] = signature(method, url, allp,
                                         c['consumer_secret'], token_secret)
    return 'OAuth ' + ', '.join(f'{_enc(k)}="{_enc(v)}"' for k, v in oauth.items())


# ---------------- 登录 ----------------

def _login_params(domain: str) -> dict:
    return {
        'clientId': CLIENT_ID,
        'locale': 'en-US',
        'service': f'https://mobile.integration.{domain}/gcm/android',
    }


def start_login(account: str, password: str, region: str = 'cn', sess=None) -> dict:
    """账号密码登录。account 可以是手机号（中国站）或邮箱。

    中国站 garmin.cn 的账号就是手机号，用户通常没设过邮箱；
    SSO 的 username 字段对手机号和邮箱一视同仁（实测两者都返回
    INVALID_USERNAME_PASSWORD，没有格式报错）。
    成功返回 tokens；需要验证码抛 MFARequired。
    """
    account = (account or '').strip()
    if not account:
        raise GarminError('请填佳明账号（手机号）')
    domain = DOMAINS.get(region or 'cn', DOMAINS['cn'])
    params = _login_params(domain)
    trace(f'start_login domain={domain} account={_mask(account)}')

    # 整个登录流程共用一个 session：预热种下的 cookie 要一直带到换票那一步
    sess = sess or net.requests.Session(impersonate='chrome')

    # 先访问登录页种 cookie（缺了它服务端会当异常请求）
    try:
        sess.get(f'https://sso.{domain}/mobile/sso/en/sign-in',
                 params={'clientId': CLIENT_ID},
                 headers={**SSO_UA, 'Sec-Fetch-Site': 'none'}, timeout=60)
    except Exception as exc:
        trace(f'  warm-up 失败（不致命）：{type(exc).__name__} {exc}')

    # Accept 必须显式要 JSON：SSO 会按 Accept 决定回 HTML 还是 JSON，
    # SSO_UA 里默认是 text/html，某些情况下会拿到一坨 HTML 导致解析失败。
    hdr = {**SSO_UA, 'Accept': 'application/json', 'Content-Type': 'application/json'}
    r = sess.post(f'https://sso.{domain}/mobile/api/login',
                  params=params, headers=hdr,
                  json={'username': account, 'password': password,
                        'rememberMe': False, 'captchaToken': ''}, timeout=60)
    trace(f'  POST /mobile/api/login -> {r.status_code} ct={r.headers.get("content-type","")}')
    try:
        j = r.json()
    except ValueError:
        trace(f'  非 JSON 响应：{r.text[:300]!r}')
        raise GarminError(f'登录接口返回非 JSON（HTTP {r.status_code}）：{r.text[:160]}')

    rs = (j.get('responseStatus') or {})
    typ = rs.get('type') or 'UNKNOWN'
    trace(f'  responseStatus={typ} msg={rs.get("message")} '
          f'captchaPassed={j.get("captchaAlreadyPassed")} '
          f'mfa={(j.get("customerMfaInfo") or {}).get("mfaLastMethodUsed")}')
    if typ == 'SUCCESSFUL':
        ticket = j.get('serviceTicketId') or ''
        trace(f'  拿到 ticket：{"有" if ticket else "空"}')
        if not ticket:
            raise GarminError(f'佳明说登录成功但没给 ticket：{str(j)[:200]}')
        return _finish(ticket, domain, sess)
    if typ == 'MFA_REQUIRED':
        info = j.get('customerMfaInfo') or {}
        raise MFARequired(domain, params,
                          info.get('mfaLastMethodUsed') or 'email', j, sess)
    if typ == 'INVALID_USERNAME_PASSWORD':
        raise GarminError('佳明账号或密码不对（INVALID_USERNAME_PASSWORD）')
    raise GarminError(f'佳明登录失败：{typ} {rs.get("message") or ""}'.strip())


class MFARequired(Exception):
    """需要输入验证码。把状态存下来，等用户填完再 continue_mfa。"""

    def __init__(self, domain: str, params: dict, method: str, raw: dict,
                 sess=None):
        super().__init__(f'需要验证码（{method}）')
        self.domain = domain
        self.params = params
        self.method = method
        self.raw = raw
        self.sess = sess
        self.created = time.time()


_PENDING_MFA: MFARequired | None = None


def pending_mfa() -> MFARequired | None:
    global _PENDING_MFA
    if _PENDING_MFA and time.time() - _PENDING_MFA.created < 600:
        return _PENDING_MFA
    return None


def continue_mfa(code: str) -> dict:
    """提交验证码。验证码 10 分钟内有效。"""
    st = pending_mfa()
    if not st:
        raise GarminError('没有待验证的登录，请重新输入账号密码')
    r = (st.sess or net.requests).post(
        f'https://sso.{st.domain}/mobile/api/mfa/verifyCode',
        params=st.params, headers={**SSO_UA, 'Accept': 'application/json'},
        json={'mfaMethod': st.method, 'mfaVerificationCode': code.strip(),
              'rememberMyBrowser': False, 'reconsentList': [],
              'mfaSetup': False}, timeout=60)
    try:
        j = r.json()
    except ValueError:
        raise GarminError(f'验证码接口返回非 JSON（HTTP {r.status_code}）')
    typ = (j.get('responseStatus') or {}).get('type')
    if typ != 'SUCCESSFUL':
        raise GarminError(f'验证码不通过：{typ} '
                          f'{(j.get("responseStatus") or {}).get("message") or ""}'.strip())
    global _PENDING_MFA
    _PENDING_MFA = None
    return _finish(j['serviceTicketId'], st.domain, st.sess)


def _finish(ticket: str, domain: str, sess=None) -> dict:
    """用 service ticket 换 OAuth1，再换 OAuth2。

    踩过的坑（实测，别再犯）：
      1. 参数必须叫 **login-url**，值就是登录时用的那个 service。
         换成 `service` 会被服务端当成「没给票」，永远 401。
      2. ticket / login-url / accepts-mfa-tokens 这三个 query 参数**必须参与签名**。
         garth 用 requests_oauthlib 时是自动算进去的，手写签名时漏了就会
         报 invalid signature —— 这是最早那次失败的原因。
      3. 服务端对不同错误给的文案很有迷惑性：假 ticket 回
         "Expected parameters oauth token or SSO ticket were not provided"，
         看着像参数没传，其实是票无效。不要拿假票的报错去反推参数名。
      4. 换票请求要和登录请求共用一个 session（cookie 连续），garth 也是这么做的。
    """
    sv = f'https://mobile.integration.{domain}/gcm/android'
    base = f'https://connectapi.{domain}/oauth-service/oauth/preauthorized'
    trace(f'  ticket 形状 len={len(ticket)} 前缀={ticket[:8]!r}')

    # login-url 是主路（garth 现行写法）；service 只作为兜底再试一次
    cands = [
        ('login-url', {'ticket': ticket, 'login-url': sv, 'accepts-mfa-tokens': 'true'}),
        ('service', {'ticket': ticket, 'service': sv, 'accepts-mfa-tokens': 'true'}),
    ]
    errs = []
    for label, q in cands:
        url = base + '?' + urlencode(q)          # ticket 是 [A-Za-z0-9-]，编码与否等价
        hdr = {**APP_UA, 'Authorization': sign('GET', url, q)}
        client = sess or net.requests
        try:
            r = client.get(url, headers=hdr, timeout=60)
        except Exception as exc:
            trace(f'  preauthorized[{label}] 异常 {type(exc).__name__} {str(exc)[:90]}')
            errs.append(f'{label}: {type(exc).__name__}')
            continue
        trace(f'  preauthorized[{label}] -> {r.status_code} {r.text[:120]!r}')
        if r.status_code >= 300:
            errs.append(f'{label}: HTTP {r.status_code} {r.text[:120]}')
            continue
        kv = dict(p.split('=', 1) for p in r.text.strip().split('&') if '=' in p)
        if 'oauth_token' not in kv:
            errs.append(f'{label}: 响应缺 oauth_token')
            continue
        oauth1 = {
            'oauth_token': kv['oauth_token'],
            'oauth_token_secret': kv.get('oauth_token_secret', ''),
            'mfa_token': kv.get('mfa_token', ''),
            'domain': domain,
        }
        return {'oauth1': oauth1, 'oauth2': exchange(oauth1, domain, sess)}

    raise GarminError('换 OAuth1 失败（ticket 不被接受）：' + ' | '.join(errs))


def exchange(oauth1: dict, domain: str, sess=None) -> dict:
    """OAuth1 -> OAuth2（access_token 有效期约 1 天，refresh 约 1 年）。"""
    url = f'https://connectapi.{domain}/oauth-service/oauth/exchange/user/2.0'
    body = {'audience': 'GARMIN_CONNECT_MOBILE_ANDROID_DI'}
    if oauth1.get('mfa_token'):
        body['mfa_token'] = oauth1['mfa_token']
    auth = sign('POST', url, body, oauth1['oauth_token'], oauth1['oauth_token_secret'])
    r = (sess or net.requests).post(
        url, data=body,
        headers={**APP_UA, 'Authorization': auth,
                 'Content-Type': 'application/x-www-form-urlencoded'}, timeout=60)
    trace(f'  exchange/2.0 -> {r.status_code} {r.text[:120]!r}')
    if r.status_code >= 300:
        raise GarminError(f'换 OAuth2 失败 HTTP {r.status_code}: {r.text[:160]}')
    j = r.json()
    j['expires_at'] = int(time.time()) + int(j.get('expires_in') or 0)
    j['refresh_token_expires_at'] = int(time.time()) + int(j.get('refresh_token_expires_in') or 0)
    j['domain'] = domain
    return j


# ---------------- 持久化与自动续期 ----------------

def save_tokens(tokens: dict) -> None:
    config.save(garmin_oauth1=json.dumps(tokens['oauth1']),
                garmin_oauth2=json.dumps(tokens['oauth2']))


def load_tokens() -> tuple[dict, dict] | None:
    c = config.load()
    try:
        o1 = json.loads(c.get('garmin_oauth1') or 'null')
        o2 = json.loads(c.get('garmin_oauth2') or 'null')
    except ValueError:
        return None
    if not o1 or not o2:
        return None
    return o1, o2


def access_token(log=None) -> tuple[str, str]:
    """返回 (domain, 可用的 access_token)，过期前自动用 OAuth1 续期。"""
    got = load_tokens()
    if not got:
        raise GarminError('还没登录佳明，请先在界面上输入邮箱和密码登录')
    o1, o2 = got
    domain = o2.get('domain') or o1.get('domain') or 'garmin.cn'
    if int(o2.get('expires_at') or 0) > time.time() + 120:
        return domain, o2['access_token']
    if log:
        log('佳明 access_token 已过期，正在续期…')
    try:
        fresh = exchange(o1, domain)
    except Exception as exc:
        raise GarminError(f'续期失败，请重新登录：{exc}')
    save_tokens({'oauth1': o1, 'oauth2': fresh})
    return domain, fresh['access_token']


def api(method: str, path: str, log=None, **kw) -> object:
    """调 connectapi。返回解析后的 JSON（204 返回 None）。"""
    domain, token = access_token(log=log)
    url = f'https://connectapi.{domain}{path}'
    h = dict(kw.pop('headers', {}) or {})
    h.update(API_UA)
    h['Authorization'] = f'Bearer {token}'
    r = net.requests.request(method, url, headers=h, timeout=kw.pop('timeout', 120), **kw)
    if r.status_code >= 300:
        raise GarminError(f'{method} {path} -> HTTP {r.status_code}: {r.text[:200]}')
    if r.status_code == 204 or not r.content:
        return None
    try:
        return r.json()
    except ValueError:
        return r.text
