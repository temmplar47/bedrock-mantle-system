# Bedrock & Mantle 多账户监控系统
# AWS Bedrock & Mantle Multi-Account Monitoring Dashboard

> English version below | 英文版在中文版之后

---

## 一、项目简介 | Project Overview

**bedrock-mantle-system** 是一套运行在本地的 AWS Bedrock / Mantle 多账户监控看板，支持：

- 🔑 多账户 AK/SK 管理（本地存储，无云端泄露）
- 🤖 模型连通性批量测试（Bedrock 20 + Mantle 44 个模型）
- 🎫 AWS Support 工单实时读取（支持中文、含最新消息时间）
- 📊 CloudWatch 指标 · 成本拆分 · 服务配额（3 条配额实时查询）
- 💓 账户健康探测（Bedrock 控制面 ListFoundationModels）
- 🖥️ 浏览器 UI（单页看板，两列布局，支持成本开关）
- ⚡ 本地 Python HTTP 服务（绕过 CORS，SSE SigV4 鉴权）

项目地址：https://github.com/temmplar47/bedrock-mantle-system

---

## 二、快速启动 | Quick Start

```bash
# 1. 克隆
git clone https://github.com/temmplar47/bedrock-mantle-system.git
cd bedrock-mantle-system

# 2. 安装依赖
pip install boto3 botocore

# 3. 启动服务（自动打开浏览器）
python ui_server.py --port 8899

# 4. 访问看板
open http://127.0.0.1:8899
```

添加账户：进入「账户」标签页 → 填写 Access Key ID / Secret Access Key → 点「添加账户」。

---

## 三、界面预览 | UI Overview

```
┌──────────────────────────────────────────────────────────┐
│  Bedrock · Mantle · 多账户监控看板          [成本] 关闭   │
├──────────┬──────────┬──────────┬──────────┬─────────────┤
│ Dashboard │ Mantle测试│ Bedrock测试│ 工单     │ 账户         │
├──────────┴──────────┴──────────┴──────────┴─────────────┤
│                                                          │
│  ┌─────────────┐  ┌─────────────┐                        │
│  │ Zerus Tech  │  │ Account-45… │                        │
│  │ Bedrock [OK]│  │ Bedrock [!] │                        │
│  │ Mantle [OK] │  │ Mantle [ERR]│                        │
│  │ [配额|健康] │  │ [配额|健康] │                        │
│  │ 工单(Urgent)│  │ 工单(None)  │                        │
│  └─────────────┘  └─────────────┘                        │
└──────────────────────────────────────────────────────────┘
```

---

## 四、API 端点 | API Endpoints

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 浏览器看板 |
| `/api/auth` | POST | 验证 AK/SK 是否有效 |
| `/api/sts-name` | POST | 获取账户名（优先 Organizations 名） |
| `/api/dashboard` | POST | 返回所有账户模型测试结果 + 工单 |
| `/api/cloudwatch` | POST | 返回 CW 指标 + 成本 + 配额 + 健康 |
| `/api/mantle` | POST | 批量测试 Mantle 模型（支持 filter 过滤） |
| `/api/bedrock` | POST | 批量测试 Bedrock 模型 |
| `/api/tickets` | POST | 读取 Support 工单（支持 status/severity 过滤） |

---

## 五、技术架构 | Architecture

```
[Browser UI]  --(localStorage 密钥)-->  [Python HTTP :8899]
                                           |
                                    [core.py SigV4]
                                           |
                                    [AWS Bedrock / Mantle API]
                                           |
                                    [AWS Support / CloudWatch / Cost Explorer]
```

**Mantle Anthropic 模型路由**：`/anthropic/v1/messages`（而非 `/v1/messages`，后者在 Mantle 网关未配置）。

**鉴权**：所有调用均通过 AWS SigV4（service=bedrock-mantle）签名，无云端中转。

---

## 六、配置文件 | Configuration

`config.json` 定义了 64 个模型的路由与参数：

```json
{
  "mantle": [
    {"id": "anthropic.claude-haiku-4-5", "route1_path": "/anthropic/v1/messages", "sigv4_service": "bedrock-mantle"},
    {"id": "openai.gpt-5.6-luna", "route1_path": "/v1/chat/completions", "route2_path": "/openai/v1/chat/completions", ...}
  ],
  "bedrock": [
    {"id": "anthropic.claude-opus-4-6-v1", "apiId": "global.anthropic.claude-opus-4-6-v1", ...},
    {"id": "openai.gpt-5.6-terra", "apiId": "global.openai.gpt-5.6-terra", ...}
  ]
}
```

---

## 七、已知问题与解决 | Known Issues & Fixes

### Mantle Haiku /v1/messages 404
**问题**：POST `/v1/messages` 返回空 404，以为是沙箱网络问题。
**根因**：Mantle 网关将 Messages 路由挂在 `/anthropic/v1/messages`，`/v1/messages` 根本未配置。
**修复**：core.py `mantle_chat` 对 anthropic 前缀模型改走 `/anthropic/v1/messages`（2026-10-26）。

### 强刷 URL 返回 404
**问题**：`do_GET` 只匹配精确路径 `/`，带查询参数 `?v=xxx` 全部返回 404。
**修复**：ui_server.py 用 `path.split("?")[0]` 归一化路径后匹配（v241026a）。

---

## License | 许可证

MIT License

---

## English Version

### AWS Bedrock & Mantle Multi-Account Monitoring Dashboard

**bedrock-mantle-system** is a local-first multi-account monitoring dashboard for AWS Bedrock and Mantle APIs. No cloud relay, no credentials stored externally — everything runs on your machine.

**Key Features:**
- 🔑 Multi-account AK/SK management (stored in browser localStorage)
- 🤖 Batch model connectivity testing (20 Bedrock + 44 Mantle models)
- 🎫 AWS Support ticket reader (with latest message time, Chinese-friendly)
- 📊 CloudWatch metrics, Cost Explorer breakdowns, Service Quotas (3 quotas live)
- 💓 Account health probe via Bedrock `ListFoundationModels`
- 🖥️ Browser UI with two-column layout, cost toggle, real-time refresh
- ⚡ Local Python HTTP server (bypasses CORS, SigV4-signed to AWS)

**Quick Start:**
```bash
pip install boto3 botocore
python ui_server.py --port 8899
# open http://127.0.0.1:8899
```

**Tech Stack:** Python 3 · boto3/botocore · SigV4 · HTML5/JS (no framework) · AWS Support API · CloudWatch GetMetricData · Cost Explorer

**Source:** https://github.com/temmplar47/bedrock-mantle-system

**Haiku Fix Story:** The Mantle gateway does NOT expose `/v1/messages` — that path returns an empty 404 at the API Gateway layer. The actual Anthropic Messages endpoint lives at `/anthropic/v1/messages` with header `anthropic-workspace-id: default` and `anthropic-version: 2026-10-21`. The fix was discovered through systematic path probing and confirmed with a real 200 response including Anthropic-formatted JSON.
