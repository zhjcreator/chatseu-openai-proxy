#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChatSEU 自动登录模块。

封装东南大学统一身份认证(SSO)登录 → 换取 ChatSEU JSESSIONID 的完整流程。
基于 fetch_lecture 项目的 seu_auth.py (作者 Golevka2001 / zhjcreator, GPL-3.0)。

核心流程:
1. seu_login() 向 auth.seu.edu.cn 发起 RSA 加密登录
2. 拿到的 ticket 重定向到 ChatSEU 回调 /api/cas/call-back
3. 回调后 ChatSEU 域下发 JSESSIONID (会话凭证)

依赖: requests + pycryptodome
"""

import os
import ssl
import requests
from requests.adapters import HTTPAdapter

from seu_auth import seu_login, get_pub_key, rsa_encrypt

# 关闭 SSL 告警 (校园网证书问题)
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass


class TLSAdapter(HTTPAdapter):
    """支持 TLSv1.2 的适配器, 解决校园网 SSL 报错。"""
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.options |= 0x4  # OP_LEGACY_SERVER_CONNECT
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


# ChatSEU 的 SSO service 回调地址 (url 参数 L2NoYXQ* 是 /chat 的 base64)
CHATSEU_SERVICE = "https://chatseu.seu.edu.cn/api/cas/call-back?url=L2NoYXQ*"


def chatseu_login(username: str, password: str, fingerprint: str = None):
    """完整登录流程, 返回 (jsessionid, cookies_dict) 或 (None, None)。

    Args:
        username: 一卡通号
        password: 密码 (明文)
        fingerprint: 设备指纹 (可选, 用于免验证码)

    Returns:
        jsessionid: ChatSEU 域的 JSESSIONID
        cookies_dict: 完整 Cookie 字典 (含 JSESSIONID 和网关 cookie)
    """
    # 1. 向统一身份认证发起登录
    session, redirect_url, error = seu_login(username, password, CHATSEU_SERVICE, fingerprint)

    if error == 'non_trusted_device':
        raise RuntimeError(
            "非可信设备登录, 需要手机验证码。请在 seu_login 中传入 mobile_verify_code, "
            "或先通过 sendStage2Code 获取验证码。"
        )
    if not session or not redirect_url:
        raise RuntimeError(f"SSO 登录失败: {error}")

    # 2. 挂 TLS 适配器, 访问回调换取 ChatSEU 的 JSESSIONID
    session.mount("https://", TLSAdapter())
    session.mount("http://", TLSAdapter())
    session.headers.pop("Content-Type", None)

    try:
        resp = session.get(redirect_url, verify=False, allow_redirects=True, timeout=60)
    except Exception as e:
        raise RuntimeError(f"访问 ChatSEU 回调失败: {e}")

    if resp.status_code != 200:
        raise RuntimeError(f"ChatSEU 回调返回异常 [{resp.status_code}]")

    # 3. 提取 ChatSEU 域的 JSESSIONID
    jsessionid = session.cookies.get("JSESSIONID", domain="chatseu.seu.edu.cn")
    if not jsessionid:
        raise RuntimeError("未能从回调获取 ChatSEU 的 JSESSIONID")

    # 收集完整 cookie
    cookies = {
        "JSESSIONID": jsessionid,
    }
    for c in session.cookies:
        if c.domain == "chatseu.seu.edu.cn" and c.name != "JSESSIONID":
            cookies[c.name] = c.value

    return jsessionid, cookies


def send_verify_code(username: str):
    """向绑定手机发送短信验证码 (用于非可信设备场景)。"""
    session, _ = get_pub_key()
    if not session:
        raise RuntimeError("获取公钥失败")
    url = "https://auth.seu.edu.cn/auth/casback/sendStage2Code"
    res = session.post(url, data={"userId": username})
    if not res.json().get("success"):
        raise RuntimeError(f"发送验证码失败: {res.json()}")
    return session


if __name__ == "__main__":
    # 独立测试: python3 chatseu_login.py <username> <password>
    import sys
    if len(sys.argv) < 3:
        print("用法: python3 chatseu_login.py <一卡通号> <密码>")
        sys.exit(1)
    jsid, cookies = chatseu_login(sys.argv[1], sys.argv[2])
    print("✅ 登录成功, JSESSIONID:", jsid)
    print("cookies:", cookies)
