# ChatSEU → OpenAI 兼容 API

把东南大学校内大模型 **ChatSEU**(https://chatseu.seu.edu.cn/chat)包装成 OpenAI 兼容接口,使 Codex CLI、Continue、Cline 及任意支持自定义 `base_url` 的 agent 可直接接入。

## 背景与原理

ChatSEU 是东南大学网络与信息中心自研的问答服务,前端为 umi(React)SPA,后端通过 Cookie 会话鉴权。其真实对话接口为:

| 步骤 | 方法 | 地址 | 说明 |
|------|------|------|------|
| 提交消息 | POST | `/api/chat/streamchat` | 返回 `messageId` |
| 流式读取 | GET | `/api/chat/streamchat?messageId=<id>` | SSE 逐字返回 |

**鉴权**:Cookie 中的 `JSESSIONID`(登录后由身份认证中心写入,HttpOnly)。

**模型映射**(`modelCode`):

| 模型名 | modelCode | 说明 |
|--------|-----------|------|
| `qwen3.5-397b` | 3 | Qwen3.5-397B,当前默认 |
| `deepseek-v4-flash` | 4 | DeepSeek-V4-Flash |
| `deepseek-r1` | 2 | DeepSeek R1(前端未暴露,后端存在) |
| `pangu` | 5 | 盘古(前端未暴露,后端存在) |

## 快速开始

### 方式一:自动登录(推荐)

配置账密后,代理启动时自动登录并刷新 JSESSIONID,失效时也会自动重登。

1. 编辑 `chatseu_config.json`,填入账密:

```json
{
  "username": "你的学号",
  "password": "你的密码",
  "default_model": "qwen3.5-397b"
}
```

2. 安装依赖:

```bash
pip install requests pycryptodome
```

3. 启动:

```bash
python3 chatseu_proxy.py --port 8000
```

启动时会自动:登录 SSO → 换取 JSESSIONID → 拉取历史会话 → 就绪。

### 方式二:手动 JSESSIONID

登录 https://chatseu.seu.edu.cn/chat 后,打开浏览器开发者工具 → Application → Cookies,复制 `chatseu.seu.edu.cn` 域名下的 `JSESSIONID` 值,填入配置:

```json
{
  "jsessionid": "你的JSESSIONID",
  "gateway_cookie": "",
  "default_model": "qwen3.5-397b"
}
```

或通过环境变量 `CHATSEU_JSESSIONID`。

### 3. 启动代理

```bash
python3 chatseu_proxy.py --port 8000
```

### 4. 接入 agent

**base_url**:`http://127.0.0.1:8000/v1`
**API Key**:任意非空值(如 `sk-chatseu`,代理不校验)

#### Codex CLI

```bash
codex --model qwen3.5-397b --base-url http://127.0.0.1:8000/v1
```

或在 `~/.codex/config.toml` 中添加 provider:

```toml
[model_providers.chatseu]
name = "ChatSEU"
base_url = "http://127.0.0.1:8000/v1"
env_key = "CHATSEU_API_KEY"

[model_providers.chatseu.models]
qwen = "qwen3.5-397b"
```

#### OpenAI SDK(Python)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="sk-chatseu",
)

resp = client.chat.completions.create(
    model="qwen3.5-397b",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

#### cURL

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.5-397b","messages":[{"role":"user","content":"你好"}]}'
```

## 接口说明

- `GET /health` — 健康检查(含活跃会话数)
- `GET /v1/models` — 模型列表
- `POST /v1/chat/completions` — 对话补全(支持 `stream: true/false`)

支持的可选字段:`model`、`messages`、`stream`、`enable_search`(联网搜索)。

## 上下文管理策略

代理内置**会话路由**,解决多轮上下文污染问题:

1. **严格前缀匹配**:每次请求的 `messages` 会与内存中已有会话的历史做逐条比对。若新请求的 messages 以某个会话历史为前缀(即本次是续写),则**复用该会话的上游 conversationID**,并只把**新增的 user 内容**发给上游(前缀部分上游已记住)。
2. **未命中则新建**:找不到前缀匹配时,新建会话并发送完整历史(拼接成首条)。
3. **system 注入**:`system` 消息在会话首条时拼接到最前,作为上下文前缀发给上游(上游不支持原生 system role)。
4. **LRU 淘汰**:内存最多保留 20 个活跃会话,超出时淘汰最久未使用的。

效果:不同话题/不同 agent 的对话**相互隔离**,同一会话的多轮**正常接续记忆**。

## 启动时拉取网页历史

代理启动时会自动从上游拉取**网页上已有的历史会话**,并恢复到内存会话池:

- 调 `GET /api/chat-history/conversation-list` 获取会话列表
- 对每个会话调 `GET /api/chat-history/conversation-content?conversationID=xxx` 获取逐轮内容
- 将每轮 `contentAsk`/`contentAnswer` 重建为 messages 历史,注入会话池

这样重启代理后,网页上聊过的对话仍可被**前缀匹配续写**,不会丢失上下文。

如需跳过拉取(从零开始),启动时加 `--no-history`:

```bash
python3 chatseu_proxy.py --no-history
```

> 注意:前缀匹配是**逐字严格匹配**。续写时,请求里的历史 messages 必须与上游记录的历史**完全一致**才能命中;若 agent 侧记忆有偏差,会安全地新建会话(避免污染)。

## 自动登录与续期

代理支持**账密自动登录**,解决 JSESSIONID 时效问题(JSESSIONID 是会话级 cookie,每次登录变化且浏览器关闭失效):

- **启动时自动登录**:配置 `username`/`password` 后,启动时自动走 SSO 登录换取新 JSESSIONID。
- **失效自动重登**:请求遇 302「请重新登录」时,自动重新登录刷新 JSESSIONID 并重试。
- **登录实现**:复用 `seu_auth.py`(源自 fetch_lecture 项目,基于 RSA 加密的纯 requests 实现),无浏览器依赖。

> 依赖 `requests` + `pycryptodome`,需 `pip install requests pycryptodome`。

登录流程:账密 → RSA 加密 → `auth.seu.edu.cn` 认证 → 拿 ticket → 访问 `/api/cas/call-back` 回调 → ChatSEU 域下发 JSESSIONID。

## 注意事项

1. **JSESSIONID 时效**:会话级 cookie,服务端空闲超时会失效。自动登录模式下,代理会失效自动重登;手动模式下需定期更新。
2. **会话状态**:代理重启时会自动从网页拉取历史恢复;运行中新建的会话若未在网页侧同步,重启后可能丢失。
3. **并发限制**:ThreadingHTTPServer 多线程处理,但上游对同一 JSESSIONID 的并发可能有排队。
4. **仅限校内网络**:代理需运行在能访问 chatseu.seu.edu.cn 的网络环境(校园网或 VPN)。
