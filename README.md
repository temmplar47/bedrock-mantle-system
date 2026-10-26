# Bedrock & Bedrock Mantle 测试系统 + AWS Support 工单读取

一体化工具:用 AWS 访问密钥验证身份、批量测试 Amazon Bedrock 原生模型与 Bedrock Mantle 模型,
并读取 AWS Support 工单详情。提供**命令行**与**浏览器 UI**两种用法。

## 目录
- `core.py` - 共用核心逻辑(凭证校验 / Mantle / Bedrock / 工单),返回纯数据,供 CLI 与 Web 共用
- `bedrock_system.py` - 命令行入口
- `ui_server.py` - 本地 Web 服务(标准库 http.server,无额外依赖)
- `static/index.html` - 单文件前端(含样式与脚本)
- `config.json` - 区域、Mantle 端点、模型清单

## 凭证(重要)
脚本**不硬编码任何密钥**。CLI 用环境变量,UI 用浏览器 localStorage。
```powershell
$env:AWS_ACCESS_KEY_ID="AKIA...."
$env:AWS_SECRET_ACCESS_KEY="...."
$env:AWS_DEFAULT_REGION="us-east-1"
```
AWS Support 接口只在 `us-east-1` / `us-west-2` 提供。

## 🖥️ 浏览器 UI（推荐）
密钥存在浏览器 localStorage（**支持同时保存多个账户**），每次请求发给本地服务，不上传任何远程服务器。
```powershell
cd bedrock-mantle-system
python3 ui_server.py            # 默认 http://127.0.0.1:8899 并自动打开浏览器
python3 ui_server.py --port 9000 --no-browser
```
打开后:
1. 「AWS 账户管理」卡片可**添加多个账户**（名称 + AK/SK/Region，添加时自动验证）;点「设为当前」切换;
2. 切到 **Mantle 模型测试** / **Bedrock 原生测试** / **工单读取** 三个选项卡，使用当前账户运行;
3. 工单选项卡有 **📨 读取所有账户工单**，一次性遍历全部已保存账户并分组展示工单。

架构:浏览器(存密钥) → 本地服务(ui_server.py) → 复用 core.py 调 AWS。
因 AWS 接口不允许浏览器跨域直连,所有 AWS 调用由本地服务完成,规避 CORS。

⚠️ 安全:localStorage 为明文,请勿在共享/公共电脑使用;测试后建议到 AWS 控制台轮转密钥。

## 命令行用法
```powershell
python3 bedrock_system.py auth                              # 验证密钥
python3 bedrock_system.py bedrock --filter qwen              # 原生 Bedrock(按子串过滤)
python3 bedrock_system.py mantle --filter gemma              # Mantle(遇 route 错误自动切 /openai/v1)
python3 bedrock_system.py tickets --filter 1787968602 --full # 工单(可含完整通信)
python3 bedrock_system.py all                                # 验证 + Bedrock + Mantle
```

## 工单字段映射(需求 8 项)
| 需求字段 | AWS Support 字段 |
|---------|-----------------|
| 主题     | subject |
| 案例 ID  | caseId (展示ID: displayId) |
| 创建时间 | timeCreated |
| 工单类型 | serviceCode |
| 状态     | status |
| 严重性   | severityCode |
| 类别     | categoryCode |
| 最新消息 | recentCommunications[0].body |

## 技术要点
- **Bedrock 原生**: boto3 `bedrock-runtime.converse`;命中 invalid/operation-not-allowed 时尝试 inference-profile 回退。
- **Bedrock Mantle**: botocore `SigV4Auth` 以 service=`bedrock-mantle` 签名 OpenAI 格式请求;
  命中 route 错误时切到 `/openai/v1/chat/completions` 重试。
- **安全提醒**:账户 457508561572 曾出现密钥泄露工单(类别 notification-exposed-key),
  AWS 已删除 IAM 用户 MantleApiKey-56h0cunr,请测试后及时轮转密钥。
