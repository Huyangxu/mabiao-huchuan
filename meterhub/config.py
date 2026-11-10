# -*- coding: utf-8 -*-
"""配置与本地库位置。

数据全部放在项目自己的 data/ 目录，与其他工具互不干扰。
可用环境变量 METERHUB_HOME 改位置。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

VERSION = "0.9"

SECRET_KEYS = (
    'xz_token', 'xz_phone', 'igp_token', 'igp_region', 'igp_phone',
    'strava_client_id', 'strava_client_secret', 'strava_refresh_token',
    'wahoo_client_id', 'wahoo_client_secret', 'wahoo_refresh_token',
    'garmin_password', 'garmin_oauth1', 'garmin_oauth2',
    'onelap_password', 'onelap_token', 'onelap_refresh',
)

DEFAULTS = {
    'xz_token': '', 'xz_phone': '',
    'igp_token': '', 'igp_region': 'cn', 'igp_phone': '',
    'strava_client_id': '', 'strava_client_secret': '',
    'strava_access_token': '', 'strava_refresh_token': '', 'strava_expires_at': 0,
    'wahoo_client_id': '', 'wahoo_client_secret': '',
    'wahoo_access_token': '', 'wahoo_refresh_token': '', 'wahoo_expires_at': 0,
    # Garmin：账号（中国站是手机号，也可填邮箱）+密码，
    # 走移动端 SSO -> OAuth1 -> OAuth2，token 缓存到下面两项
    'garmin_email': '', 'garmin_password': '', 'garmin_region': 'cn',
    'garmin_oauth1': '', 'garmin_oauth2': '',
    # 迈金/顽鹿：账号+密码，登录后拿到 token/uid/refresh
    'onelap_account': '', 'onelap_password': '',
    'onelap_token': '', 'onelap_refresh': '', 'onelap_uid': '',
    'port': 8788,
}


def home() -> Path:
    env = os.environ.get('METERHUB_HOME', '').strip()
    base = Path(env) if env else Path(__file__).resolve().parent.parent / 'data'
    base.mkdir(parents=True, exist_ok=True)
    return base


def lib_dir() -> Path:
    p = home() / 'library'
    p.mkdir(parents=True, exist_ok=True)
    return p


def cfg_path() -> Path:
    return home() / 'config.json'


def load() -> dict:
    c = dict(DEFAULTS)
    p = cfg_path()
    if p.exists():
        try:
            c.update(json.loads(p.read_text(encoding='utf-8')))
        except Exception:
            pass
    return c


def save(**kw) -> dict:
    c = load()
    c.update({k: v for k, v in kw.items() if k in DEFAULTS})
    cfg_path().write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding='utf-8')
    return c


def masked(c: dict | None = None) -> dict:
    """给界面看的状态，不含真实 token。"""
    c = c or load()
    return {
        'version': VERSION,
        'home': str(home()),
        'xz': bool(c.get('xz_token')),
        'xz_phone': c.get('xz_phone', ''),
        'igp': bool(c.get('igp_token')),
        'igp_phone': c.get('igp_phone', ''),
        'igp_region': c.get('igp_region', 'cn'),
        'garmin': bool(c.get('garmin_oauth2')),
        'garmin_email': c.get('garmin_email', ''),
        'garmin_region': c.get('garmin_region', 'cn'),
        'onelap': bool(c.get('onelap_token')),
        'onelap_account': c.get('onelap_account', ''),
        'strava': bool(c.get('strava_refresh_token') or c.get('strava_access_token')),
        'wahoo': bool(c.get('wahoo_refresh_token') or c.get('wahoo_access_token')),
    }
