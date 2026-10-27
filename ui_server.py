"""
Bedrock / Mantle UI Server
Serves the dashboard + proxies API calls to AWS using caller-supplied credentials.
"""
import json, argparse, re
from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver
from urllib.parse import urlparse
from threading import Thread
from concurrent.futures import ThreadPoolExecutor
import time
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import core

# ── Config ──────────────────────────────────────────────────────────────────
CONFIG = core.load_config()
DASH_PROMPT = "hi"

# 看板 4 模型（Mantle 短名格式，前端显示用）
DASH_MODELS = {
    "bedrock": [
        {"id": "claude.opus-4-6", "shortId": "claude.opus-4-6",
         "displayName": "Claude Opus 4.6",
         "apiId": "global.anthropic.claude-opus-4-6-v1"},
        {"id": "global.openai.gpt-5.6-terra", "shortId": "global.openai.gpt-5.6-terra",
         "displayName": "GPT-5.6 Terra",
         "apiId": "global.openai.gpt-5.6-terra"},
    ],
    "mantle": [
        {"id": "anthropic.claude-haiku-4-5", "shortId": "claude.haiku-4-5",
         "displayName": "Claude Haiku 4.5 (Mantle)",
         "apiId": "anthropic.claude-haiku-4-5"},
        {"id": "openai.gpt-5.6-luna", "shortId": "openai.gpt-5.6-luna",
         "displayName": "GPT-5.6 Luna (Mantle)",
         "apiId": "openai.gpt-5.6-luna"},
    ],
}


# ── Helpers ──────────────────────────────────────────────────────────────────
def _send_json(handler, obj, status=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler):
    try:
        length = int(handler.headers.get("Content-Length", 0))
        if length > 0:
            raw = handler.rfile.read(length)
        else:
            # 无 Content-Length（如 chunked / HTTP/1.0 无长度）时读到 EOF
            raw = handler.rfile.read()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return {}


def _get_creds(data, fallback_region="us-east-1"):
    """从请求体提取凭证（支持 {credentials:{...}} 或平铺格式）"""
    if "credentials" in data:
        c = data["credentials"]
    else:
        c = data
    return {
        "access_key": c.get("accessKeyId") or c.get("access_key") or "",
        "secret_key": c.get("secretAccessKey") or c.get("secret_key") or "",
        "session_token": c.get("sessionToken") or c.get("session_token") or "",
        "region": data.get("region") or fallback_region,
    }


def _fetch_org_account_name(account_id, ak, sk, st, region):
    """用 AWS Organizations describe_account 获取账户友好名称（如 Zerus Technology LLC）。
    无权限/未纳入组织时回退 'Account-{id}'。"""
    try:
        import boto3
        sess = boto3.Session(
            aws_access_key_id=ak or None,
            aws_secret_access_key=sk or None,
            aws_session_token=st or None,
            region_name=region or "us-east-1",
        )
        name = sess.client("organizations").describe_account(AccountId=account_id)\
            .get("Account", {}).get("Name", "")
        if name:
            return name
    except Exception:
        pass
    return f"Account-{account_id}"


def _fetch_sts_account_name(ak, sk, st, region):
    """用 STS GetCallerIdentity 获取账户信息；账户名优先取 Organizations 友好名称。"""
    try:
        import boto3
        sess = boto3.Session(
            aws_access_key_id=ak or None,
            aws_secret_access_key=sk or None,
            aws_session_token=st or None,
            region_name=region or "us-east-1",
        )
        r = sess.client("sts").get_caller_identity()
        # 账户名优先取 Organizations 账户名（如 Zerus Technology LLC）；
        # 取不到（非组织成员/无权限）时回退 Account-{id}。不取 IAM 用户名。
        account_id = r.get("Account", "")
        name = _fetch_org_account_name(account_id, ak, sk, st, region)
        return {"accountId": account_id, "userId": r.get("UserId"),
                "arn": r.get("Arn", ""), "name": name, "ok": True}
    except Exception as e:
        return {"name": ak and (ak[:8] + "..."), "ok": False, "error": str(e)}


def _perm_err(err, model_type):
    """生成人类友好的权限缺失提示"""
    if "Anthropic" in err and "not allowed" in err:
        return (f"⚠️ 当前账户未开通 Anthropic 模型权限"
                f"（需在 AWS 控制台申请 {model_type} 模型访问）\n原始错误: {err}")
    if "not available for this account" in err:
        return (f"⚠️ 当前账户未开通该模型权限"
                f"（联系 AWS Sales 申请 {model_type} 访问）\n原始错误: {err}")
    return err


def _default_models(kind):
    return CONFIG["models"].get(kind, [])


# ── 看板单账户处理 ─────────────────────────────────────────────────────────
def _run_dashboard_account(acc):
    ak = acc.get("access_key", "")
    sk = acc.get("secret_key", "")
    st = acc.get("session_token", "")
    region = acc.get("region") or CONFIG.get("region", "us-east-1")
    name = acc.get("name") or ak

    entry = {
        "name": name,
        "account": "", "userId": "", "arn": "",
        "verified": False, "error": "",
        "bedrock": {},   # {models: [{ok, text, err, elapsed, modelId, displayName}]}
        "mantle": {},
        "tickets": [],
    }

    # ── STS 验证 + 真实账户名 ──
    v = core.verify_credentials(ak, sk, st, region)
    if not v["valid"]:
        entry["error"] = v["error"]
        return entry
    entry["verified"] = True
    entry["account"] = v["account"]
    entry["userId"] = v.get("userid", "")
    entry["arn"] = v.get("arn", "")

    # 账户名优先取 Organizations 友好名称，回退 Account-{id}（不取 IAM 用户名）
    account_id = v.get("account", "")
    entry["name"] = _fetch_org_account_name(account_id, ak, sk, st, region)

    # ── Bedrock 模型测试 ──
    entry["bedrock"] = {"models": []}
    for m_def in DASH_MODELS["bedrock"]:
        api_id = m_def["apiId"]
        br = core.test_bedrock_models(ak, sk, st, region, [api_id], DASH_PROMPT, CONFIG)
        r = br[0] if br else None
        model_entry = {
            "modelId": api_id,
            "shortId": m_def["shortId"],
            "displayName": m_def["displayName"],
            "ok": False, "text": "", "err": "", "elapsed": 0, "via": "",
        }
        if r and r.get("ok"):
            model_entry.update({"ok": True, "text": r.get("text", ""),
                                 "elapsed": r.get("elapsed", 0), "via": r.get("via", "")})
        else:
            err = (r.get("err", "模型未返回结果") if r else "模型未返回结果")
            model_entry.update({"err": _perm_err(err, "Bedrock"),
                                 "elapsed": r.get("elapsed", 0) if r else 0,
                                 "via": r.get("via", "") if r else ""})
        entry["bedrock"]["models"].append(model_entry)

    # ── Mantle 模型测试 ──
    entry["mantle"] = {"models": []}
    for m_def in DASH_MODELS["mantle"]:
        api_id = m_def["apiId"]
        mt = core.test_mantle_models(ak, sk, st, region, [api_id], DASH_PROMPT, CONFIG)
        r = mt[0] if mt else None
        model_entry = {
            "modelId": api_id,
            "shortId": m_def["shortId"],
            "displayName": m_def["displayName"],
            "ok": False, "text": "", "err": "", "elapsed": 0, "route": "",
        }
        if r and r.get("ok"):
            model_entry.update({"ok": True, "text": r.get("text", ""),
                                 "elapsed": r.get("elapsed", 0), "route": r.get("route", "")})
        else:
            err = (r.get("err", "模型未返回结果") if r else "模型未返回结果")
            model_entry.update({"err": _perm_err(err, "Mantle"),
                                 "elapsed": r.get("elapsed", 0) if r else 0,
                                 "route": r.get("route", "") if r else ""})
        entry["mantle"]["models"].append(model_entry)

    # ── 工单（非 resolved）──
    tickets_raw = core.read_tickets(ak, sk, st, region, CONFIG, [], False)
    if tickets_raw.get("error"):
        entry["ticketsError"] = tickets_raw["error"]
    else:
        entry["tickets"] = [t for t in tickets_raw.get("cases", [])
                            if t.get("status") != "resolved"]

    return entry


# ── CloudWatch 数据拉取（来自 bedrock-dashboard lambda 逻辑）──────────────
def _fetch_cloudwatch_for_account(acc, session=None, include_cost=False):
    """拉取 Bedrock CloudWatch 调用指标（按模型）+ 成本，返回 {models, cost, errors}。
    若传入 session 则复用（供服务配额 / 健康探测共用一个会话）。
    注意：Cost Explorer 会计费，默认不调用（include_cost=False 时跳过）。"""
    from datetime import datetime, timedelta, timezone

    ak = acc.get("access_key", "")
    sk = acc.get("secret_key", "")
    st = acc.get("session_token", "")
    region = acc.get("region") or "us-east-1"
    window_min = int(acc.get("windowMinutes", 60))

    result = {"models": [], "cost": {}, "errors": []}
    if session is None:
        try:
            import boto3
            session = boto3.Session(
                aws_access_key_id=ak or None,
                aws_secret_access_key=sk or None,
                aws_session_token=st or None,
                region_name=region,
            )
        except Exception as e:
            result["errors"].append({"type": "session", "message": str(e)})
            return result
    sess = session  # 下面 cloudwatch / cost 沿用此会话

    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(minutes=window_min)

    # CloudWatch GetMetricData
    CW_QUERIES = [
        ("inputTokens", "InputTokenCount", "Sum"),
        ("outputTokens", "OutputTokenCount", "Sum"),
        ("invocations", "Invocations", "Sum"),
        ("latency", "InvocationLatency", "Average"),
    ]
    models_data = {}
    try:
        cw = sess.client("cloudwatch", region_name=region)
        queries = []
        for qid, metric, stat in CW_QUERIES:
            queries.append({
                "Id": qid,
                "Expression": f"SEARCH(' {{AWS/Bedrock, ModelId}} MetricName=\"{metric}\" ', '{stat}', 3600)",
                "ReturnData": True,
            })
        resp = cw.get_metric_data(
            MetricDataQueries=queries,
            StartTime=start_dt,
            EndTime=now,
            ScanBy="TimestampDescending",
        )
        for r in resp.get("MetricDataResults", []):
            label = r.get("Label", "")
            m = re.search(r"ModelId\s*=\s*([^\s,]+)", label)
            model_id = m.group(1) if m else (label.split()[0] if label else None)
            if not model_id:
                continue
            vals = [v for v in r.get("Values", []) if v is not None]
            if not vals:
                continue
            d = models_data.setdefault(model_id, {"latList": []})
            qid = r.get("Id")
            if qid == "inputTokens":
                d["inputTokens"] = d.get("inputTokens", 0) + sum(vals)
            elif qid == "outputTokens":
                d["outputTokens"] = d.get("outputTokens", 0) + sum(vals)
            elif qid == "invocations":
                d["invocations"] = d.get("invocations", 0) + sum(vals)
            elif qid == "latency":
                d["latList"].append(sum(vals) / len(vals))
    except Exception as e:
        result["errors"].append({"type": "cloudwatch", "message": str(e)})

    for model_id, d in models_data.items():
        lat = sum(d["latList"]) / len(d["latList"]) if d["latList"] else 0
        result["models"].append({
            "modelId": model_id,
            "inputTokens": int(d.get("inputTokens", 0)),
            "outputTokens": int(d.get("outputTokens", 0)),
            "invocations": int(d.get("invocations", 0)),
            "avgLatencyMs": round(lat, 2),
        })

    # Cost Explorer（独立函数，含分页 / HOURLY 回退 / 按服务拆分）
    # 默认不调用：Cost Explorer API 本身会计费，由前端开关决定是否启用。
    if include_cost:
        result["cost"] = _fetch_cost(sess, start_dt, now)
    else:
        result["cost"] = {}
        result["costSkipped"] = True

    return result


# 服务配额（bedrock-dashboard 移植）
QUOTA_SERVICE_CODE = "bedrock"
QUOTA_CODES = [
    "L-D06938E7",  # Bedrock 每分钟推理请求数 (On-Demand)
    "L-3DD46812",  # Bedrock 自定义 / 预置吞吐相关配额
    "L-D983F7C0",  # Bedrock 模型接入 / 推理相关配额
]


def _fetch_quotas(session, regions, service_code=QUOTA_SERVICE_CODE, quota_codes=QUOTA_CODES):
    """查询 Service Quotas（如 L-D06938E7）账户级配额，逐区域返回。"""
    out = []
    for region in regions:
        for code in quota_codes:
            item = {"region": region, "quotaCode": code}
            try:
                sq = session.client("service-quotas", region_name=region)
                q = sq.get_service_quota(ServiceCode=service_code, QuotaCode=code).get("Quota") or {}
                # AWS 对无量纲配额把 Unit 字段返回字面量字符串 "None"，归一化为空。
                unit = q.get("Unit")
                if unit in (None, "None", ""):
                    unit = ""
                item.update({
                    "quotaName": q.get("QuotaName"),
                    "value": q.get("Value"),
                    "unit": unit,
                    "adjustable": q.get("Adjustable"),
                })
            except Exception as e:  # noqa: BLE001 - 配额错误内联显示
                item["error"] = str(e)
            out.append(item)
    return out


def _fetch_health(session):
    """账户健康探针（只读）：Bedrock 控制面 ListFoundationModels。
    账户被风控 / 限制时会返回账户级错误；正常则返回模型清单数量。
    注：原 CloudShell 探针已不可用（botocore 1.4x 已移除 cloudshell 服务模型），
    改用同账号、同区域的 Bedrock 控制面探针。"""
    try:
        br = session.client("bedrock", region_name="us-east-1")
        n = len(br.list_foundation_models().get("modelSummaries", []))
        return {"status": "ok", "models": n}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": str(e)}


def _fetch_cost(session, start_dt, end_dt):
    """Cost Explorer：按 SERVICE 分组汇总 Bedrock 相关费用（含 marketplace 订阅）；
    跨 >2 天自动 DAILY；HOURLY 未开通时回退 DAILY 并继续分页。"""
    from datetime import datetime, timedelta, timezone
    try:
        span_days = (end_dt - start_dt).total_seconds() / 86400
        granularity = "HOURLY" if span_days <= 2 else "DAILY"
        ce = session.client("ce", region_name="us-east-1")

        def _tp(gran):
            if gran == "HOURLY":
                return {
                    "Start": start_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "End": end_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            return {
                "Start": (start_dt - timedelta(days=1)).strftime("%Y-%m-%d"),
                "End": (end_dt + timedelta(days=1)).strftime("%Y-%m-%d"),
            }

        time_period = _tp(granularity)
        totals = {}
        currency = "USD"
        token = None
        while True:
            kwargs = {
                "TimePeriod": time_period,
                "Granularity": granularity,
                "Metrics": ["UnblendedCost"],
                "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
            }
            if token:
                kwargs["NextPageToken"] = token
            try:
                resp = ce.get_cost_and_usage(**kwargs)
            except Exception as e:
                if granularity == "HOURLY" and "Hourly data granularity" in str(e):
                    granularity = "DAILY"
                    time_period = _tp("DAILY")
                    token = None
                    continue
                raise
            for r in resp.get("ResultsByTime", []):
                bstart = datetime.fromisoformat(r["TimePeriod"]["Start"].replace("Z", "+00:00"))
                if bstart.tzinfo is None:
                    bstart = bstart.replace(tzinfo=timezone.utc)
                bucket_end = bstart + (timedelta(hours=1) if granularity == "HOURLY" else timedelta(days=1))
                if bucket_end <= start_dt or bstart > end_dt:
                    continue
                for g in r.get("Groups", []):
                    name = g["Keys"][0] if g.get("Keys") else ""
                    if "bedrock" not in name.lower():
                        continue
                    m = g.get("Metrics", {}).get("UnblendedCost", {}) or {}
                    amt = m.get("Amount")
                    if amt:
                        totals[name] = totals.get(name, 0.0) + float(amt)
                    if m.get("Unit"):
                        currency = m["Unit"]
            token = resp.get("NextPageToken")
            if not token:
                break
        services = [{"name": k, "amount": round(v, 4)} for k, v in sorted(totals.items(), key=lambda x: -x[1])]
        return {"amount": round(sum(totals.values()), 4), "currency": currency, "services": services}
    except Exception as e:
        return {"amount": 0, "currency": "USD", "error": str(e)}


# ── HTTP Handler ─────────────────────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默日志

    def do_GET(self):
        # 忽略查询串（如 /?v=241024f 缓存破坏参数），按路径根匹配
        path_only = self.path.split("?", 1)[0]
        if path_only == "/" or path_only == "" or path_only.startswith("/index"):
            path = os.path.join(STATIC_DIR, "index.html")
            if os.path.exists(path):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                body = open(path, "rb").read()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404, "index.html not found")
            return
        self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST,GET,OPTIONS")
        self.end_headers()

    def do_POST(self):
        data = _read_body(self)
        parsed = urlparse(self.path)

        # ── /api/auth ──────────────────────────────────────────────────────
        if parsed.path == "/api/auth":
            c = _get_creds(data)
            v = core.verify_credentials(c["access_key"], c["secret_key"],
                                         c["session_token"], c["region"])
            if v["valid"]:
                # 账户名优先取 Organizations 友好名称，回退 Account-{id}
                name = _fetch_org_account_name(v["account"], c["access_key"],
                                               c["secret_key"], c["session_token"],
                                               c["region"])
                _send_json(self, {
                    "valid": True, "account": v["account"],
                    "userid": v.get("userid", ""), "arn": v.get("arn", ""),
                    "name": name,
                    "policies": v.get("policies", []), "error": "",
                })
            else:
                _send_json(self, {"valid": False, "error": v.get("error", "验证失败")})
            return

        # ── /api/sts-name ─────────────────────────────────────────────────
        if parsed.path == "/api/sts-name":
            c = _get_creds(data)
            r = _fetch_sts_account_name(c["access_key"], c["secret_key"],
                                          c["session_token"], c["region"])
            _send_json(self, r)
            return

        # ── /api/mantle ───────────────────────────────────────────────────
        if parsed.path == "/api/mantle":
            c = _get_creds(data)
            filt = data.get("filter") or []
            prompt = data.get("prompt") or "hi"
            models = [d["apiId"] for d in DASH_MODELS["mantle"]
                      if not filt or any(f in d["apiId"] or f in d["shortId"] for f in filt)]
            results = core.test_mantle_models(c["access_key"], c["secret_key"],
                                               c["session_token"], c["region"],
                                               models, prompt, CONFIG)
            ok = sum(1 for r in results if r["ok"])
            _send_json(self, {"total": len(results), "ok": ok, "results": results})
            return

        # ── /api/bedrock ──────────────────────────────────────────────────
        if parsed.path == "/api/bedrock":
            c = _get_creds(data)
            filt = data.get("filter") or []
            prompt = data.get("prompt") or "hi"
            models = [d["apiId"] for d in DASH_MODELS["bedrock"]
                      if not filt or any(f in d["apiId"] or f in d["shortId"] for f in filt)]
            results = core.test_bedrock_models(c["access_key"], c["secret_key"],
                                                 c["session_token"], c["region"],
                                                 models, prompt, CONFIG)
            ok = sum(1 for r in results if r["ok"])
            _send_json(self, {"total": len(results), "ok": ok, "results": results})
            return

        # ── /api/cloudwatch ───────────────────────────────────────────────
        if parsed.path == "/api/cloudwatch":
            """拉取账户 CloudWatch 指标 + 成本 + 服务配额 + 账户健康（bedrock-dashboard 全功能）"""
            # 支持单账户 {access_key:...} 或多账户 [{access_key:...},...]
            accounts_in = data if isinstance(data, list) else [data]
            out = []
            for acc in accounts_in:
                c = _get_creds(acc)
                # _get_creds 只取凭证，需从原始请求体读成本开关
                c["includeCost"] = bool(acc.get("includeCost") or acc.get("include_cost"))
                region = c.get("region") or "us-east-1"
                # 单个会话复用：cloudwatch / 配额 / 健康探测
                try:
                    import boto3
                    sess = boto3.Session(
                        aws_access_key_id=c.get("access_key") or None,
                        aws_secret_access_key=c.get("secret_key") or None,
                        aws_session_token=c.get("session_token") or None,
                        region_name=region,
                    )
                except Exception as e:
                    out.append({
                        "accountLabel": acc.get("name") or "", "accountId": "",
                        "models": [], "cost": {},
                        "quotas": [], "health": {},
                        "errors": [{"type": "session", "message": str(e)}],
                    })
                    continue
                # 成本开关：默认关闭（Cost Explorer 会计费），由前端 includeCost 控制
                include_cost = bool(c.get("includeCost") or c.get("include_cost"))
                r = _fetch_cloudwatch_for_account(c, sess, include_cost=include_cost)
                arn_info = _fetch_sts_account_name(c["access_key"], c["secret_key"],
                                                    c["session_token"], c["region"])
                quotas = _fetch_quotas(sess, [region])
                health = {"bedrock": _fetch_health(sess)}
                out.append({
                    "accountLabel": acc.get("name") or arn_info.get("name", ""),
                    "accountId": arn_info.get("accountId", ""),
                    "models": r["models"],
                    "cost": r["cost"],
                    "costSkipped": r.get("costSkipped", False),
                    "quotas": quotas,
                    "health": health,
                    "errors": r["errors"],
                })
            _send_json(self, {"accounts": out})
            return

        # ── /api/dashboard ────────────────────────────────────────────────
        if parsed.path == "/api/dashboard":
            accounts_in = data if isinstance(data, list) else [data]
            out = [_run_dashboard_account(acc) for acc in accounts_in]
            _send_json(self, {"accounts": out})
            return

        # ── /api/tickets ──────────────────────────────────────────────────
        if parsed.path == "/api/tickets":
            c = _get_creds(data)
            filt = data.get("filter") or []
            full = bool(data.get("full", False))
            out = core.read_tickets(c["access_key"], c["secret_key"],
                                     c["session_token"], c["region"],
                                     CONFIG, filt, full)
            _send_json(self, out)
            return

        # ── /api/tickets-reply ───────────────────────────────────────────
        if parsed.path == "/api/tickets-reply":
            c = _get_creds(data)
            case_id = (data.get("caseId") or "").strip()
            body = (data.get("body") or "").strip()
            if not case_id or not body:
                _send_json(self, {"ok": False, "error": "caseId 与回复内容均不能为空"}, 400)
                return
            out = core.reply_ticket(c["access_key"], c["secret_key"],
                                    c["session_token"], c["region"],
                                    CONFIG, case_id, body)
            _send_json(self, out)
            return

        # ── /api/tickets-all ──────────────────────────────────────────────
        if parsed.path == "/api/tickets-all":
            if not isinstance(data, dict):
                _send_json(self, {"error": "Invalid request body"}, 400)
                return
            accounts_in = data.get("accounts", [])
            out = []
            for acc in accounts_in:
                c = _get_creds(acc)
                name = acc.get("name") or c["access_key"]
                v = core.verify_credentials(c["access_key"], c["secret_key"],
                                             c["session_token"], c["region"])
                entry = {"name": name, "account": v.get("account", ""),
                         "error": "", "cases": [],
                         # 回显该账户凭证（仅本地浏览器↔本服务，供按账户回复使用）
                         "creds": {"access_key": c["access_key"],
                                   "secret_key": c["secret_key"],
                                   "region": c["region"]}}
                if not v["valid"]:
                    entry["error"] = v.get("error", "验证失败")
                    out.append(entry)
                    continue
                filt = acc.get("filter") or []
                full = bool(acc.get("full", False))
                t = core.read_tickets(c["access_key"], c["secret_key"],
                                       c["session_token"], c["region"],
                                       CONFIG, filt, full)
                if t.get("error"):
                    entry["error"] = t["error"]
                entry["cases"] = t.get("cases", [])
                out.append(entry)
            _send_json(self, {"accounts": out})
            return

        self.send_error(404)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True



def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8899)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Bedrock/Mantle UI 已启动: {url}")
    print("在浏览器打开上面的地址即可使用。Ctrl+C 停止。")
    if not args.no_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
