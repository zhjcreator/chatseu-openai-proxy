#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
东南大学 aTrust (深信服 Sangfor) 网关非校园网认证模块。

背景:
    非校园网(公网出口)访问 chatseu.seu.edu.cn 时, 会被 Sangine 网关拦截,
    302 重定向到 vpn.seu.edu.cn 的 aTrust 网关做身份验证。本模块实现这条
    认证链路的完整自动化:

        chatseu.seu.edu.cn (公网出口)
          └─ 302 → /controller/v1/public/verify?t=<JWT>        [网关拦截]
          └─ 302 → /portal/shortcut.html?t=<JWT>               [下发 lang cookie]
          └─ GET /passport/v1/public/authConfig                [拿认证配置]
          └─ GET /passport/v1/public/casLogin?sfDomain=CAS-auth [跳转到 CAS]
          └─ 302 → auth.seu.edu.cn/dist/#/dist/main/login?service=<vpn回调>
          └─ CAS 登录(复用 seu_auth.py) → 带 ticket 回调
          └─ GET /passport/v1/auth/cas?ticket=<CAS_TICKET>      [下发 sid/TGT 会话]
          └─ POST /controller/v1/public/reportEnv               [上报浏览器环境]
          └─ GET /passport/v1/auth/authCheck                    [ACL 校验, 轮换 sidTicket]
          └─ POST /passport/v1/public/sessionIdExchange         [兑换新会话]
          └─ 访问 chatseu → verify 自动回跳附 sdpAppCode → 放行

    认证完成后, 目标站点 chatseu.seu.edu.cn 会进入正常的 CAS 流程
    (继续走 chatseu_login.py 拿 JSESSIONID)。

依赖: requests + pycryptodome (与 chatseu_login.py 一致)。
纯标准库之外的请求走 requests, 复用项目已有 TLSAdapter 以兼容校园网证书。
"""

import re
import urllib.parse

import requests
from requests.adapters import HTTPAdapter

from seu_auth import TLSAdapter  # 复用项目已有的 TLS 兼容适配器


# 网关与目标域名
VPN_BASE = "https://vpn.seu.edu.cn"
TARGET_BASE = "https://chatseu.seu.edu.cn"

# aTrust 认证接口 (从 portal JS 逆向提取)
PATH_VERIFY = "/controller/v1/public/verify"
PATH_AUTH_CONFIG = "/passport/v1/public/authConfig"
PATH_CAS_LOGIN = "/passport/v1/public/casLogin"
PATH_CAS_CALLBACK = "/passport/v1/auth/cas"   # CAS ticket 回跳落地页
PATH_REPORT_ENV = "/controller/v1/public/reportEnv"   # 浏览器环境上报
PATH_AUTH_CHECK = "/passport/v1/auth/authCheck"       # ACL 环境校验 (GET)
PATH_ACCESS_CHECK = "/passport/v1/auth/accessCheck"   # 访问控制校验 (POST)

# 校内人员 CAS 登录域 (authConfig 的 authServerInfoList 中 authType=auth/cas 的 loginDomain)
CAS_SF_DOMAIN = "CAS-auth"


class AtrustError(RuntimeError):
    """aTrust 认证失败。"""


def _build_session(proxy=None):
    """创建带 TLS 兼容适配器、可选代理的 requests.Session。

    Args:
        proxy: HTTP/HTTPS 代理 URL (如 http://127.0.0.1:7890), 用于非校园网出口。
    """
    s = requests.Session()
    s.mount("https://", TLSAdapter())
    s.mount("http://", TLSAdapter())
    s.trust_env = False  # 不继承系统代理, 显式用传入的 proxy
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    })
    return s


def _extract_t_from_verify_url(url):
    """从 verify 跳转 URL 中提取 t 参数。"""
    q = urllib.parse.urlparse(url).query
    params = urllib.parse.parse_qs(q)
    return params.get("t", [None])[0]


def verify_gateway(session):
    """步骤 1-2: 访问 verify, 走完跳转拿到网关会话 (lang cookie)。

    返回 t 参数 (后续 authConfig 需要)。
    """
    # 先访问目标, 触发网关拦截, 拿到 verify 跳转
    r = session.get(TARGET_BASE + "/chat", allow_redirects=False, timeout=30, verify=False)

    t = None
    loc = r.headers.get("Location", "")

    # 方式一: 302 跳转, verify 地址在 Location 头
    if PATH_VERIFY in loc:
        t = _extract_t_from_verify_url(loc)

    # 方式二: 200 + JS 跳转, verify 地址在 HTML body 的 locationUrl 变量里
    if not t:
        m = re.search(r'locationUrl\s*=\s*"([^"]+)"', r.text or "")
        if m:
            loc = m.group(1)
            t = _extract_t_from_verify_url(loc)

    if not t:
        raise AtrustError(f"未能从网关响应中解析 verify 跳转 (status={r.status_code})")

    # 访问 verify (跟随跳转到 shortcut.html, 建立网关会话)
    r = session.get(VPN_BASE + PATH_VERIFY, params={"t": t},
                    allow_redirects=True, timeout=30, verify=False)
    return t


def get_auth_config(session):
    """步骤 3: 获取 aTrust 认证配置 (pubKey/guid/antiReplayRand 等)。"""
    r = session.get(VPN_BASE + PATH_AUTH_CONFIG, timeout=30, verify=False)
    data = r.json()
    if data.get("code") != 0:
        raise AtrustError(f"authConfig 失败: {data}")
    return data.get("data", {})


def cas_login_url(session):
    """步骤 4: 触发 CAS 登录, 返回 (auth.seu.edu.cn 登录地址, aTrust CAS 回调 service)。"""
    r = session.get(VPN_BASE + PATH_CAS_LOGIN, params={"sfDomain": CAS_SF_DOMAIN},
                    allow_redirects=False, timeout=30, verify=False)
    loc = r.headers.get("Location", "")

    # service 可能在 query (普通跳转) 或 hash 后的 query (SPA 路由 #/...?service=)
    service = ""
    # 先从整个 URL 里正则提取 service (兼容 hash 路由)
    m = re.search(r'[?&]service=([^&]+)', loc)
    if m:
        service = urllib.parse.unquote(m.group(1))
    # 兜底: 标准 query 解析
    if not service:
        parsed = urllib.parse.urlparse(loc)
        q = urllib.parse.parse_qs(parsed.query)
        service = q.get("service", [""])[0]
    return loc, service


def exchange_cas_ticket(session, ticket, sf_domain=CAS_SF_DOMAIN):
    """步骤 6: 携带 CAS ticket 访问回调, 让 aTrust 下发 sidTicket 会话。

    返回 (status_code, 重定向地址, 会话 Cookie)。
    支持两种 ticket 传递方式: URL 参数 ticket= 或 code=。
    """
    url = VPN_BASE + PATH_CAS_CALLBACK
    r = session.get(url, params={"sfDomain": sf_domain, "ticket": ticket},
                    allow_redirects=True, timeout=60, verify=False)
    return r


def report_browser_env(session, inner_ticket, device_id=None):
    """步骤 6.5: 上报浏览器环境 (reportEnv)。

    纯浏览器模式下, CAS 认证后需上报环境信息, 服务端才继续放行 ACL 校验。

    Args:
        session: 已持有 sid/TGT 会话的 requests.Session
        inner_ticket: CAS 回调 shortcut URL data 字段里的内部 ticket (<unitid>_<uuid>)
        device_id: 设备标识 (可选, 默认生成一个浏览器型 device_id)
    """
    import hashlib
    import uuid as _uuid
    if not device_id:
        device_id = hashlib.md5(("browser" + str(_uuid.uuid4())).encode()).hexdigest()
    body = {
        "ticket": inner_ticket,
        "deviceId": device_id,
        "env": {"endpoint": {"device_id": device_id, "device": {"type": "browser"}}},
    }
    r = session.post(VPN_BASE + PATH_REPORT_ENV, json=body,
                     allow_redirects=True, timeout=60, verify=False)
    if r.status_code != 200 or r.json().get("code") != 0:
        raise AtrustError(f"reportEnv 上报失败: {r.status_code} {r.text[:200]}")
    return r


def parse_shortcut_data(final_response):
    """从 CAS 回调的 shortcut.html URL 解析出内部 ticket 与 nextService。

    返回 (inner_ticket, next_service, query_dict)。
    """
    import json
    p = urllib.parse.urlparse(final_response.url)
    q = urllib.parse.parse_qs(p.query)
    raw = q.get("data", [""])[0]
    data_obj = json.loads(urllib.parse.unquote(raw)) if raw else {}
    inner_ticket = data_obj.get("ticket", "")
    next_service = q.get("nextService", [""])[0]
    return inner_ticket, next_service, q


def auth_check(session):
    """步骤 7: ACL 校验 (authCheck)。

    关键: 只需 `clientType=SDPBrowserClient` 一个 query 参数。
    (多带 platform/lang 会触发 422 invalid_param in query)

    返回新轮换的 sidTicket。
    """
    r = session.get(VPN_BASE + PATH_AUTH_CHECK,
                    params={"clientType": "SDPBrowserClient"},
                    allow_redirects=False, timeout=30, verify=False)
    if r.status_code != 200:
        raise AtrustError(f"authCheck 失败: {r.status_code} {r.text[:200]}")
    data = r.json()
    if data.get("code") != 0:
        raise AtrustError(f"authCheck 返回错误: {data}")
    return data.get("data", {}).get("sidTicket", "")


def exchange_session(session, sid_ticket):
    """步骤 8: 用 authCheck 轮换出的 sidTicket 兑换新会话 (sessionIdExchange)。

    兑换后 session 的 sid Cookie 会更新, 网关据此放行目标站点。
    """
    r = session.post(VPN_BASE + "/passport/v1/public/sessionIdExchange",
                     json={"sidTicket": sid_ticket},
                     allow_redirects=False, timeout=30, verify=False)
    if r.status_code != 200:
        raise AtrustError(f"sessionIdExchange 失败: {r.status_code} {r.text[:200]}")
    data = r.json()
    if data.get("code") != 0:
        raise AtrustError(f"sessionIdExchange 返回错误: {data}")
    return r


def atrust_authenticate(username, password, proxy=None, fingerprint=None,
                        mobile_verify_code=None):
    """完整非校园网 aTrust 认证流程。

    打通 chatseu.seu.edu.cn 的非校园网访问, 返回:
        (requests.Session, atrust_ticket)
    其中 session 已携带 aTrust 会话 Cookie, 可直接用于访问 chatseu.seu.edu.cn
    (后续再由 chatseu_login.py 走正常 CAS 拿 JSESSIONID)。

    Args:
        username: 一卡通号
        password: 密码
        proxy: 公网出口代理 (如 http://127.0.0.1:7890)
        fingerprint: 设备指纹 (可选)
        mobile_verify_code: 手机验证码 (非可信设备场景)
    """
    session = _build_session(proxy)

    # 1-2. 走 verify 建立网关会话
    t = verify_gateway(session)

    # 3. 拿认证配置
    cfg = get_auth_config(session)

    # 4. 触发 CAS 登录跳转, 得到 aTrust 的 CAS 回调 service
    _, service = cas_login_url(session)
    if not service:
        raise AtrustError("未能从 casLogin 跳转中解析 service 回调地址")

    # 5. 用 seu_auth.py 的 CAS 登录, service 指向 aTrust 回调
    from seu_auth import seu_login
    auth_session, redirect_url, err = seu_login(
        username, password, service, fingerprint, mobile_verify_code, proxy=proxy)
    if err == 'non_trusted_device':
        raise AtrustError("非可信设备, 需要手机验证码 (请提供 mobile_verify_code)")
    if not auth_session or not redirect_url:
        raise AtrustError(f"CAS 登录失败: {err}")

    # redirect_url 是 CAS 认证成功后要访问的 service 地址 (含 ticket),
    # 形如 https://vpn.seu.edu.cn/passport/v1/auth/cas?sfDomain=CAS-auth&ticket=ST-xxx
    cas_ticket = _extract_cas_ticket(redirect_url)

    # 6. 用 CAS 认证会话访问回调 (ticket 与该 session 的 cookie 绑定),
    #    一路经 aTrust 下发 sid/TGT 会话后跳回 shortcut.html。
    auth_session.mount("https://", TLSAdapter())
    auth_session.mount("http://", TLSAdapter())
    if proxy:
        auth_session.proxies = {"http": proxy, "https": proxy}
    final = auth_session.get(redirect_url, verify=False,
                             allow_redirects=True, timeout=60)

    # 6.5 上报浏览器环境 (reportEnv)
    inner_ticket, next_service, _ = parse_shortcut_data(final)
    if inner_ticket:
        report_browser_env(auth_session, inner_ticket)

    # 7. ACL 校验 (authCheck), 拿轮换后的新 sidTicket
    new_sid = auth_check(auth_session)
    if not new_sid:
        raise AtrustError("authCheck 未返回 sidTicket")

    # 8. 兑换新会话 (sessionIdExchange), 更新 sid Cookie
    exchange_session(auth_session, new_sid)

    # 认证完成, 返回已持有完整 aTrust 会话的 session。
    # 后续访问 chatseu.seu.edu.cn 时, 网关 verify 会自动回跳并附 sdpAppCode 放行。
    return auth_session, cas_ticket, final


def _extract_cas_ticket(url):
    """从 CAS 回跳 URL 提取 ticket (支持 ticket= / code= 参数)。"""
    parsed = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(parsed.query)
    for key in ("ticket", "code"):
        if q.get(key):
            return q[key][0]
    # 兜底: 直接在原始串里正则找
    m = re.search(r"(?:ticket|code)=([^&]+)", url)
    return m.group(1) if m else None


def detect_atrust_redirect(resp_or_headers):
    """判断响应是否为 aTrust 网关拦截 (302 到 vpn.seu.edu.cn)。

    供 chatseu_proxy.py 在直连上游遇拦截时调用。
    """
    loc = ""
    if hasattr(resp_or_headers, "headers"):
        loc = resp_or_headers.headers.get("Location", "")
    elif isinstance(resp_or_headers, dict):
        loc = resp_or_headers.get("Location", "") or resp_or_headers.get("location", "")
    return "vpn.seu.edu.cn" in loc or "aTrust" in loc or PATH_VERIFY in loc


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("用法: python3 atrust_auth.py <一卡通号> <密码> [代理URL]")
        sys.exit(1)
    u, p = sys.argv[1], sys.argv[2]
    proxy = sys.argv[3] if len(sys.argv) > 3 else None
    sess, ticket, final = atrust_authenticate(u, p, proxy=proxy)
    print("✅ aTrust 认证成功, CAS ticket:", (ticket or "")[:12], "...")
    print("会话 Cookie:", dict(sess.cookies))
