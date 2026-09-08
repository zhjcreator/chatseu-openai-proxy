#!/bin/bash
# 启动 ChatSEU OpenAI 兼容代理
cd "$(dirname "$0")"
exec /Users/zhj/.workbuddy/binaries/python/versions/3.13.12/bin/python3 chatseu_proxy.py --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
