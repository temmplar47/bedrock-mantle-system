---
title: 从"全失败"到调通：我如何用 SigV4 + 本地代理打通 AWS Mantle Haiku
author: temmplar47
date: 2026-10-26
tags: [AWS, Bedrock, Mantle, SigV4, Python, API Gateway, Anthropic]
---

# 从"全失败"到调通：AWS Mantle Haiku 路由踩坑全记录

> **English version below — scroll down for the full English article.**

---

## 背景：多账户管理之痛

做 AWS Bedrock / Mantle 开发时，最烦的不是写代码，而是**账户管理**：

- 多个客户的 AWS 账号散落在各处，切换凭证频繁出错
- 不同账号的模型授权不一样，测一个模型要来回换 AK/SK
- 工单、成本、配额散落在 AWS 控制台各个角落，切换页面繁琐
- 想批量测一批模型是否可达，没有趁手的工具

于是我写了这套**本地多账户监控看板**（bedrock-mantle-system）：

```
浏览器(localStorage存密钥) → 本地Python服务(127.0.0.1:8899) → AWS SigV4签名 → Bedrock/Mantle API
```

代码已开源：https://github.com/temmplar47/bedrock-mantle-system

---

## 功能一览

### 1. 多账户管理
支持批量导入账户（`AK SK` 或 `名称,AK,SK`），每行一个，自动 STS 验证，账户名优先从 AWS Organizations 读取真实公司名（如"Zerus Technology LLC"）。

### 2. 模型批量测试
- **Bedrock**：20 个模型，走 `/v1/chat/completions` OpenAI 兼容路由
- **Mantle**：44 个模型，走 `/v1/chat/completions` + `/openai/v1/chat/completions` 双路由自动回退
- **Mantle Anthropic 模型**（Haiku 等）：走 `/anthropic/v1/messages`（这个路径是踩坑后发现的关键，后面细说）

### 3. 工单管理
直读 AWS Support API，支持按状态/严重级别过滤，工单时间取**最新消息时间**（而非建单时间），方便追踪"对方上周回复了什么"。

### 4. CloudWatch + 成本 + 配额
- CloudWatch GetMetricData：Bedrock 调用量/错误率
- Cost Explorer：按服务拆分（Amazon Bedrock + Marketplace），可按需关闭
- Service Quotas：3 条关键配额实时查询（Fable 5 tokens/min、Opus 4.6 V1 requests/min、GPT-5.6 Luna tokens/min）

### 5. 账户健康探测
用 Bedrock 控制面 `ListFoundationModels` 作为探针，成功返回模型数量，异常时报账户级限制。

---

## 核心踩坑：Mantle Haiku "全失败"之谜

### 现象

测试账户 `678080238013`（Zerus Technology LLC）的 Mantle Haiku（`anthropic.claude-haiku-4-5`），页面显示：

```
route: /v1/messages
err: HTTP 404 (空响应体) — Messages 路由未返回内容
```

**失败原因不明**。以为是本机网络限制（走了代理），或者模型授权没开。

### 错误假设一：沙箱网络问题

测了多种 Header 组合：
- `anthropic-workspace-id: default`
- `anthropic-version: 2023-06-01`（旧版）→ 404
- `anthropic-version: 2026-10-21`（SDK 抓包得到的版本）→ 空 404
- `anthropic-project: default` → 空 404
- `x-api-key: default` → 空 404

**全失败**。但诡异的是，OpenAI 路由（`/v1/chat/completions`）能返回正常 JSON（400 带消息体），说明**出网是通的**，只是 Messages 路径不返回体。

### 错误假设二：Signature 签名问题

想当然认为是 SigV4 签名范围不对，加了 `anthropic-*` 头后签名被网关拒绝。试了用 Bearer dummy + SigV4 混合（模仿 SDK），还是空 404。

### 真相：逐路径探测

用 Python 写了脚本，系统性地测试 Mantle 网关的所有可能路径：

```python
paths_to_test = [
    '/v1/messages',
    '/anthropic/v1/messages',
    '/messages',
    '/bedrock-mantle/v1/messages',
    '/openai/v1/chat/completions',
]

for path in paths_to_test:
    r = sigv4_post(path, body, headers)
    print(path, '=>', r.status_code, bool(r.headers.get('anthropic-ratelimit-*')))
```

结果：

| 路径 | HTTP 状态 | 响应头特征 |
|------|-----------|-----------|
| `/v1/messages` | 404 | `x-amzn-requestid`（API GW 层"路由不存在"） |
| `/messages` | 404 | 同上 |
| `/bedrock-mantle/v1/messages` | 404 | 同上 |
| **`/anthropic/v1/messages`** | **200** | `application/json`，正常 Anthropic Messages 体 |

### 关键发现

**`/v1/messages` 根本没有配置在 Mantle 网关上**。AWS API Gateway 层直接返回"路由不存在"（空 404），而不是在应用层报错。

Anthropic Messages 路由实际挂在 **`/anthropic/v1/messages`**。官方 SDK（`AnthropicBedrockMantle`）内部做了路径重写，所以用户感知不到这个细节。

同时，模型 ID 必须带 `anthropic.` 前缀：`anthropic.claude-haiku-4-5` ✅，裸 `claude-haiku-4-5` ❌（404 "model does not exist"）。

### 修复代码

```python
# core.py - mantle_chat 函数中，对 anthropic 前缀模型：
paths = [(
    "/anthropic/v1/messages",   # 不是 /v1/messages！
    {
        "anthropic-workspace-id": "default",
        "anthropic-version": "2026-10-21",
        "accept": "application/json"
    }
)]

# 响应解析
data = json.loads(r.read())
text = "".join(b.get("text", "")
               for b in data.get("content", [])
               if b.get("type") == "text")
```

### 验证结果

```
/api/mantle (filter: anthropic.claude-haiku-4-5)
→ ok: True
→ route: /anthropic/v1/messages
→ text: # Hey there! 👋

How's it going? What can I help you with today?
```

---

## 另一个坑：强刷 URL 404

在修 Haiku 期间，每次 Ctrl+Shift+R 强刷 `http://127.0.0.1:8899/?v=241024f`，页面始终显示空白。

后来发现：`do_GET` 只匹配 `self.path == "/"` 精确路径，`?v=xxx` 查询参数让路径变成 `/` → **整个看板返回 Python http.server 的默认 404 页**，永远看不到新代码。

修复：
```python
def do_GET(self):
    path_only = self.path.split("?", 1)[0]  # 忽略查询参数
    if path_only == "/" or path_only.startswith("/index"):
        # serve index.html
```

**教训**：所有带 cache-bust 参数的强刷 URL（`?v=xxx`）在修这个 Bug 之前都是 404，你看到的始终是旧页面。难怪"修了还是失败"。

---

## 架构设计亮点

### 1. 本地代理绕过 CORS

AWS API 不支持浏览器 CORS，直接从浏览器调用会失败。方案：本地 Python 服务（端口 8899）接收浏览器的请求，做 SigV4 签名后转发给 AWS，返回结果给浏览器。**密钥只存在本地 localStorage**，不经过任何云端中转。

### 2. SigV4 服务名必须是 `bedrock-mantle`

用 `bedrock` 或 `execute-api` 签名都会报 401：
```
Credential should be scoped to correct service: 'bedrock-mantle'
```

### 3. 空响应体错误处理

某些路径（如 `/v1/messages`）在网关层 404 但响应体为空，直接 `json.loads("")` 会抛异常。需要在 `except` 中检查 `raw.strip()` 是否为空，并返回明确的 HTTP 状态码信息。

---

## 开源地址

🔗 https://github.com/temmplar47/bedrock-mantle-system

```bash
# 克隆 & 启动
git clone https://github.com/temmplar47/bedrock-mantle-system.git
cd bedrock-mantle-system
pip install boto3 botocore
python ui_server.py --port 8899
# 浏览器打开 http://127.0.0.1:8899
```

---

## 总结

1. **Mantle Anthropic Messages 路由**：`/anthropic/v1/messages`，不是 `/v1/messages`
2. **模型 ID 前缀**：`anthropic.claude-haiku-4-5`，带 `anthropic.` 前缀
3. **版本头**：`anthropic-version: 2026-10-21`（而非旧的 2023-06-01）
4. **本地代理**：解决 CORS + 保护密钥安全
5. **排查网络问题**：逐路径探测是最快的方式，别猜

---

*如果你也有 AWS Bedrock/Mantle 开发需求，或对 SigV4 签名、API Gateway 路由探测有兴趣，欢迎交流。*

---

---

# From "All Failed" to Working: My Journey Debugging AWS Mantle Haiku Routing

## Background

When managing multiple AWS Bedrock/Mantle accounts, the most frustrating part isn't writing code — it's **account management**:

- Multiple customer accounts scattered across AWS, switching credentials is error-prone
- Different accounts have different model authorizations, testing one model means constant AK/SK swapping
- Tickets, costs, and quotas are spread across AWS Console, requiring constant tab-switching
- No handy tool to batch-test whether a list of models is reachable

So I built a **local multi-account monitoring dashboard** (bedrock-mantle-system):

```
Browser (keys in localStorage) → Local Python service (127.0.0.1:8899) → AWS SigV4 signed → Bedrock/Mantle API
```

Open source: https://github.com/temmplar47/bedrock-mantle-system

---

## Features

### 1. Multi-Account Management
Batch import accounts (`AK SK` or `Name,AK,SK` format), auto-validate via STS, account name from AWS Organizations (e.g. "Zerus Technology LLC").

### 2. Batch Model Testing
- **Bedrock**: 20 models, OpenAI-compatible `/v1/chat/completions`
- **Mantle**: 44 models, dual-route auto-fallback (`/v1/chat/completions` → `/openai/v1/chat/completions`)
- **Mantle Anthropic models** (Haiku etc.): `/anthropic/v1/messages` (key routing fix discovered through debugging — more on this below)

### 3. Support Ticket Reader
Directly reads AWS Support API, filters by status/severity, uses **latest message time** (not ticket creation time) for easier tracking.

### 4. CloudWatch + Cost + Quotas
- CloudWatch GetMetricData: Bedrock invocation counts and error rates
- Cost Explorer: service-level breakdown (Amazon Bedrock + Marketplace), toggleable on/off
- Service Quotas: 3 live quota queries (Fable 5 tokens/min, Opus 4.6 V1 requests/min, GPT-5.6 Luna tokens/min)

### 5. Account Health Probe
Uses Bedrock control plane `ListFoundationModels` as a probe — returns model count on success, flags account-level restrictions on failure.

---

## The Core Bug: Mantle Haiku "All Failed"

### Symptom

Testing account `678080238013` (Zerus Technology LLC) on Mantle Haiku (`anthropic.claude-haiku-4-5`), the UI showed:

```
route: /v1/messages
err: HTTP 404 (empty response) — Messages route returned no content
```

**No clear error message.** Suspected: sandbox network restrictions or missing model authorization.

### Wrong Assumption #1: Sandbox Network Issue

Tried various header combinations:
- `anthropic-workspace-id: default`
- `anthropic-version: 2023-06-01` (old) → 404
- `anthropic-version: 2026-10-21` (SDK-captured) → empty 404
- `anthropic-project: default` → empty 404
- `x-api-key: default` → empty 404

**All failed.** But the OpenAI route (`/v1/chat/completions`) returned proper JSON (400 with message body), proving **outbound network works** — just the Messages path returns no body.

### Wrong Assumption #2: Signature Problem

Assumed SigV4 signature scope was wrong with `anthropic-*` headers. Tried Bearer dummy + SigV4 hybrid (mimicking the SDK). Still empty 404.

### Truth: Systematic Path Probing

Wrote a Python script to systematically probe every possible Mantle gateway path:

```python
paths_to_test = [
    '/v1/messages',
    '/anthropic/v1/messages',
    '/messages',
    '/bedrock-mantle/v1/messages',
]

for path in paths_to_test:
    r = sigv4_post(path, body, headers)
    print(path, '=>', r.status_code, bool(r.headers.get('anthropic-ratelimit-*')))
```

Results:

| Path | HTTP Status | Response Header Signature |
|------|-------------|--------------------------|
| `/v1/messages` | 404 | `x-amzn-requestid` (API GW layer "route not found") |
| `/messages` | 404 | Same |
| `/bedrock-mantle/v1/messages` | 404 | Same |
| **`/anthropic/v1/messages`** | **200** | `application/json`, valid Anthropic Messages body |

### Key Findings

**`/v1/messages` is simply not configured on the Mantle gateway.** AWS API Gateway returns "route not found" (empty 404) at the gateway layer, not an application-level error.

The Anthropic Messages route lives at **`/anthropic/v1/messages`**. The official SDK (`AnthropicBedrockMantle`) internally rewrites the path — users never see this detail.

Also: model ID **must include the `anthropic.` prefix**: `anthropic.claude-haiku-4-5` ✅, bare `claude-haiku-4-5` ❌ (404 "model does not exist").

### The Fix

```python
# core.py - mantle_chat: for anthropic-prefixed models:
paths = [(
    "/anthropic/v1/messages",  # NOT /v1/messages!
    {
        "anthropic-workspace-id": "default",
        "anthropic-version": "2026-10-21",
        "accept": "application/json"
    }
)]

# Response parsing
data = json.loads(r.read())
text = "".join(b.get("text", "")
               for b in data.get("content", [])
               if b.get("type") == "text")
```

### Verification

```
/api/mantle (filter: anthropic.claude-haiku-4-5)
→ ok: True
→ route: /anthropic/v1/messages
→ text: # Hey there! 👋

How's it going? What can I help you with today?
```

---

## Another Bug: Hard Refresh URL 404

Every `Ctrl+Shift+R` hard refresh on `http://127.0.0.1:8899/?v=241024f` showed a blank page.

Root cause: `do_GET` matched only `self.path == "/"` exactly. With `?v=xxx` query string, the path becomes `/` → **the entire dashboard returned Python http.server's default 404 page**. The new code was never loaded.

**Lesson**: All the hard refresh URLs with `?v=xxx` were 404s before this fix. You were always seeing the old page. That's why "I fixed it but it still failed."

---

## Architecture Highlights

### 1. Local Proxy Bypasses CORS

AWS API doesn't support browser CORS. Solution: local Python service (port 8899) receives browser requests, applies SigV4 signature, forwards to AWS, returns result to browser. **Keys only exist in local localStorage**, never relayed through any cloud.

### 2. SigV4 Service Name Must Be `bedrock-mantle`

Using `bedrock` or `execute-api` for signing results in 401:
```
Credential should be scoped to correct service: 'bedrock-mantle'
```

### 3. Empty Response Body Error Handling

Some paths (like `/v1/messages`) return gateway-layer 404 with empty body. `json.loads("")` raises an exception. Always check `raw.strip()` in `except` and return explicit HTTP status code info.

---

## Open Source

🔗 https://github.com/temmplar47/bedrock-mantle-system

```bash
# Clone & run
git clone https://github.com/temmplar47/bedrock-mantle-system.git
cd bedrock-mantle-system
pip install boto3 botocore
python ui_server.py --port 8899
# Open http://127.0.0.1:8899 in browser
```

---

## TL;DR

1. **Mantle Anthropic Messages route**: `/anthropic/v1/messages`, not `/v1/messages`
2. **Model ID prefix**: `anthropic.claude-haiku-4-5`, include the `anthropic.` prefix
3. **Version header**: `anthropic-version: 2026-10-21` (not the old 2023-06-01)
4. **Local proxy**: solves CORS + keeps keys secure
5. **Network debugging**: systematic path probing beats guessing every time

---

*Feel free to reach out if you have AWS Bedrock/Mantle development needs, or want to discuss SigV4 signing, API Gateway routing, or API debugging techniques.*

**#AWSBedrock #Mantle #SigV4 #Python #Anthropic #APIGateway**
