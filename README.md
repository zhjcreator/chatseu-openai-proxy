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

## 非校园网:aTrust 网关自动认证

非校园网(公网出口)访问 `chatseu.seu.edu.cn` 时,会被东南大学的 **aTrust 网关**(深信服 Sangfor,`vpn.seu.edu.cn`)拦截,302 跳转到认证页。本代理支持**自动打通这条认证链路**。

### 认证原理

```
chatseu.seu.edu.cn (公网出口)
  └─ 302 → /controller/v1/public/verify?t=<JWT>          [网关拦截]
  └─ 302 → /portal/shortcut.html?t=<JWT>                 [建立网关会话]
  └─ GET /passport/v1/public/authConfig                  [拿认证配置]
  └─ GET /passport/v1/public/casLogin?sfDomain=CAS-auth  [触发 CAS 跳转]
  └─ 302 → auth.seu.edu.cn/dist/#/dist/main/login         [统一身份认证]
  └─ CAS 登录(复用 seu_auth.py) → 带 ticket 回调
  └─ GET /passport/v1/auth/cas?ticket=<CAS_TICKET>        [下发 sid/TGT 会话]
  └─ POST /controller/v1/public/reportEnv                 [上报浏览器环境]
  └─ GET /passport/v1/auth/authCheck                      [ACL 校验, 轮换 sidTicket]
  └─ POST /passport/v1/public/sessionIdExchange           [兑换新会话]
  └─ 携带 aTrust Cookie + JSESSIONID 访问上游 → 放行
```

核心机制:东南大学的 aTrust 配置了 **CAS 单点登录联动**——"校内人员"登录域(`auth/cas`)会把用户重定向到 `auth.seu.edu.cn` 做统一身份认证,认证成功后 aTrust 下发 `sidTicket` 会话放行。这与代理已有的 CAS 登录**同源**,复用 `seu_auth.py`。

关键细节:
- **authCheck 只需 `clientType=SDPBrowserClient` 一个参数**(多带 `platform`/`lang` 会触发 422)。
- 网关对**每个 URL 路径**都做 verify,访问上游 API 时需**同时携带 aTrust Cookie + JSESSIONID**,并跟随 307→verify→回跳(附 `sdpAppCode`)的重定向链。

### 使用方式

1. 配置账密 + 公网出口代理:

```json
{
  "username": "你的学号",
  "password": "你的密码",
  "proxy": "http://127.0.0.1:7890"
}
```

2. 启动时加 `--proxy`(或直接读配置):

```bash
python3 chatseu_proxy.py --proxy http://127.0.0.1:7890
```

启动时会自动:走 aTrust 认证链路 → 打通非校园网访问 → 正常 CAS 登录拿 JSESSIONID。

### 两种触发方式

- **启动预认证**:配置了 `proxy` + 账密时,启动即完成 aTrust 认证。
- **运行时自动触发**:上游请求遇 302 `vpn.seu.edu.cn` 拦截时,自动触发 `atrust_auth.py` 认证后重试。

### 单独测试认证模块

```bash
python3 atrust_auth.py <一卡通号> <密码> http://127.0.0.1:7890
```

> 依赖 `requests` + `pycryptodome`(与自动登录一致)。若遇"非可信设备"需手机验证码,需在 `atrust_auth.py` 传入 `mobile_verify_code`。
