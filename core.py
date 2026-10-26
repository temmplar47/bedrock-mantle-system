#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core.py — 被 CLI (bedrock_system.py) 与 Web 服务 (ui_server.py) 共用的核心逻辑。
所有函数都接收凭证作为参数(不依赖环境变量), 返回纯数据结构, 便于序列化给前端。
"""
import os
import json
import time
import urllib.request
import urllib.error

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# 1) 验证凭证
# --------------------------------------------------------------------------- #
def verify_credentials(ak, sk, st, region):
    st = st or None
    try:
        client = boto3.client(
            "sts", region_name=region,
            aws_access_key_id=ak, aws_secret_access_key=sk, aws_session_token=st,
        )
        ident = client.get_caller_identity()
        policies = []
        try:
            iam = boto3.client(
                "iam", region_name=region,
                aws_access_key_id=ak, aws_secret_access_key=sk, aws_session_token=st,
            )
            uname = ident["Arn"].split("/")[-1]
            pol = iam.list_attached_user_policies(UserName=uname)
            policies = [p["PolicyName"] for p in pol["AttachedPolicies"]]
        except Exception:
            pass
        return {"valid": True, "account": ident["Account"], "userid": ident["UserId"],
                "arn": ident["Arn"], "policies": policies, "error": ""}
    except ClientError as e:
        return {"valid": False, "account": "", "userid": "", "arn": "",
                "policies": [], "error": e.response["Error"].get("Message", str(e))}
    except Exception as e:
        return {"valid": False, "account": "", "userid": "", "arn": "",
                "policies": [], "error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# 2) 原生 Bedrock
# --------------------------------------------------------------------------- #
def _resolve_inference_profile(client, model_id):
    try:
        profiles = client.list_inference_profiles().get("inferenceProfileSummaries", [])
        for p in profiles:
            name = p.get("modelName") or ""
            pid = p.get("inferenceProfileId") or ""
            if model_id == pid or model_id == name:
                return p.get("inferenceProfileArn")
        for p in profiles:
            name = p.get("modelName") or ""
            if model_id and model_id in name:
                return p.get("inferenceProfileArn")
    except Exception:
        pass
    return None


def test_bedrock_models(ak, sk, st, region, model_ids, prompt, cfg):
    st = st or None
    client = boto3.client(
        "bedrock-runtime", region_name=region,
        aws_access_key_id=ak, aws_secret_access_key=sk, aws_session_token=st,
        config=Config(connect_timeout=cfg["bedrock"]["timeout"],
                      read_timeout=cfg["bedrock"]["timeout"]),
    )
    max_tokens = cfg["bedrock"]["max_tokens"]
    results = []
    for mid in model_ids:
        t0 = time.time()
        rec = {"model": mid, "ok": False, "text": "", "elapsed": 0.0, "err": "", "via": "direct"}
        try:
            resp = client.converse(
                modelId=mid,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": max_tokens},
            )
            rec["text"] = "".join(b.get("text", "") for b in
                                  resp.get("output", {}).get("message", {}).get("content", []))[:160]
            rec["ok"] = True
        except ClientError as e:
            err = e.response["Error"]
            code, msg = err.get("Code", ""), err.get("Message", "")
            if code in ("ValidationException", "AccessDeniedException") and (
                "invalid" in msg.lower() or "not allowed" in msg.lower() or "operation" in msg.lower()
            ):
                arn = _resolve_inference_profile(client, mid)
                if arn:
                    try:
                        resp = client.converse(
                            modelId=arn,
                            messages=[{"role": "user", "content": [{"text": prompt}]}],
                            inferenceConfig={"maxTokens": max_tokens},
                        )
                        rec["text"] = "".join(b.get("text", "") for b in
                                              resp.get("output", {}).get("message", {}).get("content", []))[:160]
                        rec["ok"] = True
                        rec["via"] = "inference-profile"
                    except ClientError as e2:
                        rec["err"] = f"{e2.response['Error'].get('Code','')}: {e2.response['Error'].get('Message','')}"
                        rec["via"] = "inference-profile"
            if not rec["ok"]:
                if code == "ValidationException" and ("invalid" in msg.lower() or "not allowed" in msg.lower()):
                    rec["err"] = f"该模型ID在原生 Bedrock 不可用(可能为 Mantle 专属模型): {msg}"
                else:
                    rec["err"] = f"{code}: {msg}"
        except Exception as e:
            rec["err"] = f"{type(e).__name__}: {e}"
        rec["elapsed"] = round(time.time() - t0, 2)
        results.append(rec)
    return results


# --------------------------------------------------------------------------- #
# 3) Bedrock Mantle (SigV4 service=bedrock-mantle, OpenAI 格式)
# --------------------------------------------------------------------------- #
def mantle_chat(m, model_id, prompt, ak, sk, region, timeout):
    host = m["host"]
    base = f"https://{host}"
    # Anthropic 系模型走 Messages API（与 anthropic SDK 一致）：
    #   POST /v1/messages, body {model,max_tokens,messages},
    #   header anthropic-workspace-id: default。
    # 其它模型走 OpenAI 兼容路由（/v1/chat/completions 失败则回退 /openai/v1/chat/completions）。
    # 不发送 temperature：部分模型（如 openai.gpt-5.6-luna）仅支持默认值，
    # 且 bedrock-runtime 的某些模型（如 global.openai.gpt-5.6-terra）完全不支持该字段。
    is_anthropic = model_id.startswith("anthropic.")
    if is_anthropic:
        # Mantle 网关的 Anthropic Messages 路由实际挂在 /anthropic/v1/messages
        #（/v1/messages 在网关层未配置，返回空 404），与官方 SDK 重写后的路径一致。
        # anthropic-version 用网关当前版本 2026-10-21。
        paths = [("/anthropic/v1/messages",
                   {"anthropic-workspace-id": "default",
                    "anthropic-version": "2026-10-21",
                    "accept": "application/json"})]
        body = json.dumps({
            "model": model_id,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
    else:
        paths = [(m["route1_path"], {}), (m["route2_path"], {})]
        body = json.dumps({
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
    creds = Credentials(ak, sk, None)
    svc = m["sigv4_service"]
    t0 = time.time()
    for path, extra_headers in paths:
        url = base + path
        req = AWSRequest(method="POST", url=url, data=body,
                         headers={"Content-Type": "application/json", "Host": host,
                                  **extra_headers})
        SigV4Auth(creds, svc, region).add_auth(req)
        p = req.prepare()
        ur = urllib.request.Request(p.url, data=p.body, method="POST")
        for k, v in p.headers.items():
            ur.add_header(k, v)
        try:
            r = urllib.request.urlopen(ur, timeout=timeout)
            data = json.loads(r.read().decode("utf-8"))
            if is_anthropic:
                # Anthropic Messages 响应：{"content":[{"type":"text","text":"..."}]}
                blocks = data.get("content", [])
                text = "".join(b.get("text", "") for b in blocks
                               if isinstance(b, dict) and b.get("type") == "text")
                if not text:
                    text = json.dumps(data)[:160]
            else:
                text = ""
                try:
                    text = data["choices"][0]["message"]["content"]
                except Exception:
                    text = json.dumps(data)[:160]
            return {"ok": True, "text": text[:160], "elapsed": round(time.time() - t0, 2),
                    "route": path, "err": ""}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                j = json.loads(raw)
                msg = j.get("error", {}).get("message", raw)
                code = j.get("error", {}).get("code", "")
            except Exception:
                msg, code = raw, ""
            # OpenAI 路由：模型不在该路由上时回退到下一路由
            if (not is_anthropic and (
                    "isn't supported on this route" in msg
                    or "not supported on this route" in msg
                    or "does not support the '/v1/chat/completions' API" in msg
                    or (code == "validation_error" and "does not support" in msg))):
                continue
            # 空响应体（如某些环境对 /v1/messages 返回空 404）时，明确标注 HTTP 状态码
            if not raw.strip():
                detail = f"HTTP {e.code} (空响应体)"
                if is_anthropic:
                    detail += " — Messages 路由未返回内容，可能该网络/网关不支持此路径"
                return {"ok": False, "text": "", "elapsed": round(time.time() - t0, 2),
                        "route": path, "err": detail}
            return {"ok": False, "text": "", "elapsed": round(time.time() - t0, 2),
                    "route": path, "err": f"{code}: {msg}"}
        except Exception as e:
            return {"ok": False, "text": "", "elapsed": round(time.time() - t0, 2),
                    "route": path, "err": f"{type(e).__name__}: {e}"}
    return {"ok": False, "text": "", "elapsed": round(time.time() - t0, 2),
            "route": "both", "err": "两个路由均失败 (route error 且重试后仍失败)"}


def test_mantle_models(ak, sk, st, region, model_ids, prompt, cfg):
    m = cfg["mantle"]
    timeout = m["timeout"]
    results = []
    for mid in model_ids:
        r = mantle_chat(m, mid, prompt, ak, sk, region, timeout)
        r["model"] = mid
        results.append(r)
    return results


# --------------------------------------------------------------------------- #
# 4) 读取 AWS Support 工单
# --------------------------------------------------------------------------- #
def read_tickets(ak, sk, st, region, cfg, case_filter=None, full=False):
    st = st or None
    support_region = cfg.get("support_region", region)
    client = boto3.client(
        "support", region_name=support_region,
        aws_access_key_id=ak, aws_secret_access_key=sk, aws_session_token=st,
    )
    try:
        resp = client.describe_cases(
            includeResolvedCases=cfg["tickets"]["include_resolved"],
            includeCommunications=True,
            maxResults=cfg["tickets"]["max_results"],
        )
    except ClientError as e:
        err = e.response["Error"]
        if err.get("Code") in ("SubscriptionRequiredException", "MissingSubscription"):
            return {"error": "AWS Support 不可用: 当前账户未开通 Support 计划 (Business/Enterprise)。",
                    "cases": []}
        return {"error": f"读取工单失败: {err.get('Message', e)}", "cases": []}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "cases": []}

    cases = resp.get("cases", [])
    if case_filter:
        cases = [c for c in cases if any(
            f in (c.get("caseId", "") + c.get("displayId", "") + c.get("subject", ""))
            for f in case_filter)]

    out = []
    for c in cases:
        comms = c.get("recentCommunications", {}).get("communications", [])
        latest = comms[0]["body"] if comms else "(无消息)"
        latest_time = comms[0].get("timeCreated", "") if comms else ""
        rec = {
            "subject": c.get("subject", ""),
            "caseId": c.get("caseId", ""),
            "displayId": c.get("displayId", ""),
            "timeCreated": c.get("timeCreated", ""),
            "latestMessageTime": latest_time or c.get("timeCreated", ""),
            "serviceCode": c.get("serviceCode", ""),
            "status": c.get("status", ""),
            "severityCode": c.get("severityCode", ""),
            "categoryCode": c.get("categoryCode", ""),
            "latestMessage": latest.strip(),
        }
        out.append(rec)

    # 完整通信记录
    if full and case_filter and len(out) == 1:
        cid = out[0]["caseId"]
        try:
            cr = client.describe_cases(caseIdList=[cid], includeCommunications=True, maxResults=100)
            for cs in cr.get("cases", []):
                recs = []
                for comm in cs.get("recentCommunications", {}).get("communications", []):
                    recs.append({
                        "time": comm.get("timeCreated", ""),
                        "from": comm.get("submittedBy", ""),
                        "body": comm.get("body", "").strip(),
                    })
                out[0]["communications"] = recs
        except Exception as e:
            out[0]["communicationsError"] = str(e)
    return {"error": "", "cases": out}
