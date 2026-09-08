#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChatSEU -> OpenAI 兼容 API 代理

把东南大学 ChatSEU (https://chatseu.seu.edu.cn/chat) 包装成 OpenAI 兼容接口,
使 Codex CLI / Continue / 任意支持自定义 base_url 的 agent 可直接接入。

用法:
    python3 chatseu_proxy.py --host 127.0.0.1 --port 8000

环境变量 / 配置文件 (chatseu_config.json):
    JSESSIONID      登录后 chatseu.seu.edu.cn 的 JSESSIONID (必填)
    GATEWAY_COOKIE  网关 cookie (可选, 通常 JSESSIONID 足够)
    DEFAULT_MODEL   默认模型名, 见 MODEL_MAP

纯标准库实现, 无第三方依赖。
"""

import json
import argparse
import time
import uuid
import re
import threading
import urllib.request
import urllib.error
import urllib.parse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------- 配置

CONFIG_PATH = Path(__file__).parent / "chatseu_config.json"

# modelCode <-> 模型名映射 (从前端 JS 提取)
MODEL_MAP = {
    "deepseek-r1":     2,   # dsR1
    "qwen3.5-397b":    3,   # q32  (默认, 当前最强)
    "deepseek-v4-flash": 4, # dsV3
    "pangu":           5,   # pangu
}
MODEL_CODE_TO_NAME = {v: k for k, v in MODEL_MAP.items()}

UPSTREAM_BASE = "https://chatseu.seu.edu.cn"
POST_PATH = "/api/chat/streamchat"

DEFAULT_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "Accept": "application/json",
    "Referer": "https://chatseu.seu.edu.cn/chat",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
}


def load_config() -> dict:
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] 读取配置失败: {e}")
    # 环境变量覆盖
    import os
    cfg["jsessionid"] = os.environ.get("CHATSEU_JSESSIONID", cfg.get("jsessionid", ""))
    cfg["gateway_cookie"] = os.environ.get("CHATSEU_GATEWAY_COOKIE", cfg.get("gateway_cookie", ""))
    cfg["default_model"] = os.environ.get("CHATSEU_DEFAULT_MODEL", cfg.get("default_model", "qwen3.5-397b"))
    cfg["username"] = os.environ.get("CHATSEU_USERNAME", cfg.get("username", ""))
    cfg["password"] = os.environ.get("CHATSEU_PASSWORD", cfg.get("password", ""))
    return cfg


def build_cookie(cfg: dict) -> str:
    parts = []
    if cfg.get("jsessionid"):
        parts.append(f"JSESSIONID={cfg['jsessionid']}")
    if cfg.get("gateway_cookie"):
        parts.append(cfg["gateway_cookie"])
    return "; ".join(parts)


def auto_login(cfg: dict):
    """用账密自动登录, 刷新 JSESSIONID 并回写配置。

    返回更新后的 cookie 字符串。
    """
    username = cfg.get("username")
    password = cfg.get("password")
    if not username or not password:
        return build_cookie(cfg)

    try:
        from chatseu_login import chatseu_login
        jsid, cookies = chatseu_login(username, password, cfg.get("fingerprint"))
        cfg["jsessionid"] = jsid
        # 更新网关 cookie
        gw = next((v for k, v in cookies.items() if k != "JSESSIONID"), "")
        if gw:
            cfg["gateway_cookie"] = f"{next(iter([k for k in cookies if k != 'JSESSIONID']), '')}={gw}"
        print(f"[ok] 自动登录成功, JSESSIONID={jsid[:8]}...")
        # 回写配置文件 (保留账密)
        try:
            CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    except Exception as e:
        print(f"[warn] 自动登录失败, 使用手动 JSESSIONID: {e}")

    return build_cookie(cfg)


# ---------------------------------------------------------------- 异常

class SessionExpiredError(RuntimeError):
    """上游会话 (JSESSIONID) 失效。"""


# ---------------------------------------------------------------- 上游调用

def post_message(cookie: str, conversation_id: str, model_code: int,
                 content: str, enable_search: bool = False,
                 file_ids: list = None) -> str:
    """POST 提交消息, 返回 messageId。"""
    body = {
        "conversationID": conversation_id,
        "modelCode": model_code,
        "content": content,
        "typeCode": 0,
        "fileIds": file_ids or [],
        "enableSearch": enable_search,
    }
    data = json.dumps(body).encode("utf-8")
    headers = dict(DEFAULT_HEADERS)
    headers["Cookie"] = cookie
    req = urllib.request.Request(UPSTREAM_BASE + POST_PATH, data=data,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 302 / 401 等: 会话失效, 尝试自动重登
        if e.code in (302, 401, 403):
            raise SessionExpiredError(str(e))
        raise
    # 返回体也可能包含"请重新登录"
    if isinstance(result, dict) and "请重新登录" in str(result.get("message", "")):
        raise SessionExpiredError(str(result))
    if result.get("code") != 0:
        raise RuntimeError(f"上游错误: {result}")
    return result["response"]  # messageId


def stream_events(cookie: str, message_id: str):
    """GET SSE 流式读取, 逐条 yield 正文文本片段 (过滤 metadata / 结束标记)。"""
    url = f"{UPSTREAM_BASE}{POST_PATH}?messageId={message_id}"
    headers = {
        "Accept": "text/event-stream",
        "Referer": DEFAULT_HEADERS["Referer"],
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "Cookie": cookie,
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            # 跳过 metadata 元信息 (形如 {"now":..., "uuid":...} 的 JSON)
            if payload.startswith("{") and '"now"' in payload:
                continue
            yield payload


# ---------------------------------------------------------------- 历史会话拉取

def fetch_conversation_list(cookie: str):
    """GET 会话列表, 返回 [{conversationId, conversationName, ...}]。"""
    url = UPSTREAM_BASE + "/api/chat-history/conversation-list"
    headers = {
        "Accept": "application/json",
        "Referer": DEFAULT_HEADERS["Referer"],
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "Cookie": cookie,
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if result.get("code") != 0:
        raise RuntimeError(f"拉取会话列表失败: {result}")
    return result.get("response", [])


def fetch_conversation_content(cookie: str, conversation_id: str):
    """GET 会话内容, 返回按轮次排列的 [{contentAsk, contentAnswer, conversationRounds}]。"""
    url = (UPSTREAM_BASE + "/api/chat-history/conversation-content"
           + f"?conversationID={urllib.parse.quote(conversation_id)}")
    headers = {
        "Accept": "application/json",
        "Referer": DEFAULT_HEADERS["Referer"],
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "Cookie": cookie,
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if result.get("code") != 0:
        raise RuntimeError(f"拉取会话内容失败: {result}")
    return result.get("response", [])


def build_history_from_rounds(rounds):
    """把会话内容轮次重建为 messages 历史 key (role, content) 元组列表。

    每轮 contentAsk 作为 user, contentAnswer 作为 assistant。
    """
    history = []
    for r in rounds:
        ask = (r.get("contentAsk") or "").strip()
        ans = (r.get("contentAnswer") or "").strip()
        if ask:
            history.append(("user", ask))
        if ans:
            history.append(("assistant", ans))
    return tuple(history)


def load_history_sessions(cookie: str):
    """启动时拉取网页已有历史会话, 重建会话池。

    返回 {conversation_id: history_key}。
    """
    sessions = {}
    try:
        conv_list = fetch_conversation_list(cookie)
    except Exception as e:
        print(f"[warn] 拉取历史会话列表失败 (跳过): {e}")
        return sessions

    for conv in conv_list:
        cid = conv.get("conversationId")
        if not cid:
            continue
        try:
            rounds = fetch_conversation_content(cookie, cid)
        except Exception as e:
            print(f"[warn] 拉取会话 {cid[:8]} 内容失败 (跳过): {e}")
            continue
        history_key = build_history_from_rounds(rounds)
        if history_key:
            sessions[cid] = history_key
        print(f"[ok] 恢复会话 {cid[:8]}... ({len(history_key)} 条消息) "
              f"{conv.get('conversationName', '')[:20]}")

    return sessions


# ---------------------------------------------------------------- OpenAI 兼容层

def sse_field(name, value):
    """生成标准 SSE data 行。name 为空时只输出 data 字段。"""
    if name:
        return f"data: {json.dumps({name: value}, ensure_ascii=False)}\n\n".encode("utf-8")
    return f"data: {json.dumps(value, ensure_ascii=False)}\n\n".encode("utf-8")


def make_chunk(cid, content):
    return sse_field("choices", [{
        "delta": {"content": content},
        "index": 0,
        "finish_reason": None,
    }]) + b"data: [DONE]\n\n"


class ChatSEUProxy(BaseHTTPRequestHandler):
    cfg = {}
    cookie = ""
    # 会话管理: conversation_id -> {history: [messages], last_used: ts}
    # 用 OrderedDict 实现 LRU; 键为代理自生成的上游 conversationID
    sessions = OrderedDict()
    sessions_lock = threading.Lock()
    max_sessions = 20

    def log_message(self, fmt, *args):
        pass  # 静默

    # ---- 工具
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    @staticmethod
    def _normalize_content(c):
        """把 content 归一化成纯文本 (兼容字符串 / 多模态数组)。"""
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "".join(p.get("text", "") for p in c if p.get("type") == "text")
        return ""

    @staticmethod
    def _messages_to_key(messages):
        """把 messages 序列化成可比较的 key (用于严格前缀匹配)。"""
        return tuple(
            (m.get("role", "user"), ChatSEUProxy._normalize_content(m.get("content", "")))
            for m in messages
        )

    def _resolve_model(self, name):
        key = (name or "").lower().strip()
        if key in MODEL_MAP:
            return MODEL_MAP[key]
        # 数字直接透传
        if key.isdigit():
            return int(key)
        # 默认
        return MODEL_MAP.get(ChatSEUProxy.cfg.get("default_model", "qwen3.5-397b"), 3)

    @classmethod
    def _find_session_by_prefix(cls, msg_key):
        """严格前缀匹配: 返回 (conversation_id, 前缀长度) 或 (None, 0)。

        找到 history 是 msg_key 前缀的会话 (即本次请求是该会话的续写)。
        取匹配前缀最长者。
        """
        best_cid = None
        best_len = 0
        for cid, sess in cls.sessions.items():
            hist_key = sess["history_key"]
            # hist 是否为 msg_key 的前缀
            if len(hist_key) <= len(msg_key) and msg_key[:len(hist_key)] == hist_key:
                if len(hist_key) > best_len:
                    best_len = len(hist_key)
                    best_cid = cid
        return best_cid, best_len

    @classmethod
    def _touch_session(cls, cid, history_key):
        """更新会话历史并刷新 LRU 位置。"""
        with cls.sessions_lock:
            cls.sessions[cid] = {"history_key": history_key, "last_used": time.time()}
            cls.sessions.move_to_end(cid)

    @classmethod
    def _new_session(cls, history_key):
        """新建会话, 超上限时淘汰最久未用 (LRU)。"""
        cid = str(uuid.uuid4())
        with cls.sessions_lock:
            cls.sessions[cid] = {"history_key": history_key, "last_used": time.time()}
            cls.sessions.move_to_end(cid)
            while len(cls.sessions) > cls.max_sessions:
                cls.sessions.popitem(last=False)  # 淘汰最旧
        return cid

    @classmethod
    def _seed_session(cls, cid, history_key):
        """把上游已有会话加载进会话池 (保留原 conversationId)。"""
        with cls.sessions_lock:
            cls.sessions[cid] = {"history_key": history_key, "last_used": time.time()}
            cls.sessions.move_to_end(cid)

    # ---- 路由
    def do_GET(self):
        if self.path == "/v1/models":
            return self.handle_models()
        if self.path == "/health" or self.path == "/":
            return self._json({
                "status": "ok",
                "models": list(MODEL_MAP.keys()),
                "active_sessions": len(ChatSEUProxy.sessions),
            })
        self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        if self.path in ("/v1/chat/completions", "/chat/completions"):
            return self.handle_chat()
        self._json({"error": {"message": "not found"}}, 404)

    # ---- 模型列表
    def handle_models(self):
        data = [{"id": name, "object": "model", "owned_by": "chatseu"}
                for name in MODEL_MAP.keys()]
        self._json({"object": "list", "data": data})

    # ---- 对话补全
    def handle_chat(self):
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            return self._json({"error": {"message": "invalid json"}}, 400)

        messages = body.get("messages", [])
        if not messages:
            return self._json({"error": {"message": "messages required"}}, 400)

        # 归一化并拆出 system
        normalized = []
        system_text = ""
        for m in messages:
            role = m.get("role", "user")
            content = self._normalize_content(m.get("content", ""))
            if role == "system":
                system_text += (content + "\n")
            else:
                normalized.append((role, content))

        msg_key = tuple(normalized)
        model_name = body.get("model", "")
        model_code = self._resolve_model(model_name)
        stream = bool(body.get("stream", False))
        enable_search = bool(body.get("enable_search", False))

        # ---- 前缀匹配路由
        cid, prefix_len = self._find_session_by_prefix(msg_key)

        if cid is not None:
            # 命中: 续写已有会话, 只发送增量 (前缀之后新增的 user 内容)
            delta = normalized[prefix_len:]
        else:
            # 未命中: 新建会话, 发送完整历史 (拼接成首条)
            cid = self._new_session(msg_key)
            delta = normalized

        # 组装要发送给上游的文本
        # - 增量里只取 user 内容 (assistant 是模型已回复的, 上游已记住)
        # - system 前缀在首次建立会话时注入
        new_user_parts = [c for role, c in delta if role == "user"]
        if not new_user_parts:
            return self._json({"error": {"message": "no new user message"}}, 400)

        # system 注入: 仅当这是会话首条 (未命中新建) 时, 拼在最前
        if prefix_len == 0 and system_text:
            new_user_parts = [system_text.strip()] + new_user_parts

        content = "\n\n".join(new_user_parts)

        try:
            mid = post_message(ChatSEUProxy.cookie, cid,
                               model_code, content, enable_search)
        except SessionExpiredError:
            # 会话失效, 自动重登后重试一次
            print("[warn] JSESSIONID 失效, 尝试自动重登...")
            try:
                ChatSEUProxy.cookie = auto_login(ChatSEUProxy.cfg)
                mid = post_message(ChatSEUProxy.cookie, cid,
                                   model_code, content, enable_search)
            except Exception as e2:
                return self._json({"error": {"message": f"relogin failed: {e2}"}}, 502)
        except Exception as e:
            return self._json({"error": {"message": f"upstream error: {e}"}}, 502)

        # 更新会话历史 (记录本次完整 messages, 供下次前缀匹配)
        self._touch_session(cid, msg_key)

        if not stream:
            # 非流式: 拼接所有片段一次性返回
            chunks = []
            try:
                for piece in stream_events(ChatSEUProxy.cookie, mid):
                    chunks.append(piece)
            except Exception as e:
                return self._json({"error": {"message": f"stream error: {e}"}}, 502)
            full = "".join(chunks)
            return self._json({
                "id": f"chatcmpl-{mid}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model_name or ChatSEUProxy.cfg.get("default_model"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": full},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        # 流式 (SSE) —— 严格 OpenAI chunk 格式
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            cid = f"chatcmpl-{mid}"
            created = int(time.time())
            model_out = model_name or ChatSEUProxy.cfg.get("default_model")
            for piece in stream_events(ChatSEUProxy.cookie, mid):
                chunk = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_out,
                    "choices": [{
                        "delta": {"content": piece},
                        "index": 0,
                        "finish_reason": None,
                    }],
                }
                self.wfile.write(sse_field("", chunk))
                self.wfile.flush()
            # 结束 chunk
            final_chunk = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_out,
                "choices": [{
                    "delta": {},
                    "index": 0,
                    "finish_reason": "stop",
                }],
            }
            self.wfile.write(sse_field("", final_chunk))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
        except Exception as e:
            try:
                self.wfile.write(sse_field("error", {"message": str(e)}))
                self.wfile.write(b"data: [DONE]\n\n")
                self.close_connection = True
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser(description="ChatSEU OpenAI-compatible proxy")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-history", action="store_true",
                    help="启动时不拉取网页已有历史会话")
    args = ap.parse_args()

    cfg = load_config()
    ChatSEUProxy.cfg = cfg
    ChatSEUProxy.cookie = build_cookie(cfg)

    # 若配置了账密, 启动时自动登录刷新 JSESSIONID
    if cfg.get("username") and cfg.get("password"):
        print("[info] 检测到账密配置, 自动登录...")
        ChatSEUProxy.cookie = auto_login(cfg)

    if not cfg.get("jsessionid"):
        print("[warn] 未配置 JSESSIONID, 上游会 302 重登录。")
        print("[warn] 请在 chatseu_config.json 或环境变量 CHATSEU_JSESSIONID 中填入。")

    # 启动时拉取网页已有历史会话
    if not args.no_history and cfg.get("jsessionid"):
        print("[info] 正在从网页拉取历史会话...")
        loaded = load_history_sessions(ChatSEUProxy.cookie)
        for cid, hist_key in loaded.items():
            ChatSEUProxy._seed_session(cid, hist_key)
        print(f"[info] 已恢复 {len(loaded)} 个历史会话")

    print(f"[ok] 代理启动: http://{args.host}:{args.port}")
    print(f"[ok] base_url = http://{args.host}:{args.port}/v1")
    print(f"[ok] 可用模型: {list(MODEL_MAP.keys())}")
    print(f"[ok] 会话策略: 严格前缀匹配 + LRU(上限 {ChatSEUProxy.max_sessions})")

    server = ThreadingHTTPServer((args.host, args.port), ChatSEUProxy)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[bye] 代理已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
