#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bedrock_system.py — 命令行入口(复用 core.py 的核心逻辑)。
子命令: auth / bedrock / mantle / tickets / all
凭证从环境变量读取: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, [AWS_SESSION_TOKEN], AWS_DEFAULT_REGION
"""
import os
import sys
import argparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import core

CONFIG = core.load_config()


def _creds():
    ak = os.environ.get("AWS_ACCESS_KEY_ID")
    sk = os.environ.get("AWS_SECRET_ACCESS_KEY")
    st = os.environ.get("AWS_SESSION_TOKEN")
    if not ak or not sk:
        raise SystemExit("✗ 未检测到 AWS 凭证。请先设置环境变量 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY。")
    region = os.environ.get("AWS_DEFAULT_REGION", CONFIG["region"])
    return ak, sk, st, region


def cmd_auth():
    ak, sk, st, region = _creds()
    r = core.verify_credentials(ak, sk, st, region)
    if r["valid"]:
        print("✓ 凭证有效")
        print("  Account :", r["account"])
        print("  UserId  :", r["userid"])
        print("  Arn     :", r["arn"])
        print("  Policies:", ", ".join(r["policies"]) or "(none)")
        return True
    print("✗ 凭证无效:", r["error"])
    return False


def cmd_mantle(models_filter, prompt):
    ak, sk, st, region = _creds()
    models = CONFIG["models"]["mantle"]
    if models_filter:
        models = [m for m in models if any(f in m for f in models_filter)]
    print(f"=== 测试 Bedrock Mantle 模型 (共 {len(models)} 个) ===\n")
    results = core.test_mantle_models(ak, sk, st, region, models, prompt, CONFIG)
    r1 = r2 = 0
    for i, r in enumerate(results, 1):
        status = "✓ OK" if r["ok"] else "✗ FAIL"
        tag = ""
        if r["ok"] and "/openai" in (r.get("route") or ""):
            tag = "  (备用路由 /openai/v1)"; r2 += 1
        elif r["ok"]:
            r1 += 1
        print(f"[{i}/{len(results)}] {status} {r['model']}  ({r['elapsed']}s){tag}")
        print(f"      ↳ {r['text']!r}" if r["ok"] else f"      ↳ {r['err']}")
    ok = sum(1 for r in results if r["ok"])
    print(f"\n--- 汇总: {ok}/{len(results)} 成功  (route1={r1}, route2={r2}) ---")


def cmd_bedrock(models_filter, prompt):
    ak, sk, st, region = _creds()
    models = CONFIG["models"]["bedrock"]
    if models_filter:
        models = [m for m in models if any(f in m for f in models_filter)]
    print(f"=== 测试原生 Bedrock 模型 (共 {len(models)} 个) ===\n")
    results = core.test_bedrock_models(ak, sk, st, region, models, prompt, CONFIG)
    for i, r in enumerate(results, 1):
        status = "✓ OK" if r["ok"] else "✗ FAIL"
        via = f"  [via {r.get('via','direct')}]" if r["ok"] and r.get("via") != "direct" else ""
        print(f"[{i}/{len(results)}] {status} {r['model']}  ({r['elapsed']}s){via}")
        print(f"      ↳ {r['text']!r}" if r["ok"] else f"      ↳ {r['err']}")
    ok = sum(1 for r in results if r["ok"])
    print(f"\n--- 汇总: {ok}/{len(results)} 成功 ---")


def cmd_tickets(case_filter, full):
    ak, sk, st, region = _creds()
    out = core.read_tickets(ak, sk, st, region, CONFIG, case_filter, full)
    if out.get("error"):
        print("✗", out["error"]); return
    cases = out["cases"]
    if not cases:
        print("✓ 没有匹配的工单。"); return
    print(f"=== AWS Support 工单 (共 {len(cases)} 条) ===\n")
    for c in cases:
        print(f"● 主题      : {c['subject']}")
        print(f"  案例ID    : {c['caseId']}  (展示ID: {c['displayId']})")
        print(f"  创建时间  : {c['timeCreated']}")
        print(f"  工单类型  : {c['serviceCode']}")
        print(f"  状态      : {c['status']}")
        print(f"  严重性    : {c['severityCode']}")
        print(f"  类别      : {c['categoryCode']}")
        print(f"  最新消息  : {c['latestMessage'][:160]}")
        print()
    if full and case_filter and len(cases) == 1 and cases[0].get("communications"):
        print("=== 完整通信记录 ===")
        for cm in cases[0]["communications"]:
            print(f"  [{cm['time']}] {cm['from']}:")
            print("   ", cm["body"])
            print()


def main():
    p = argparse.ArgumentParser(description="Bedrock / Bedrock Mantle 测试 + 工单读取")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="验证 AWS 访问密钥")
    pb = sub.add_parser("bedrock", help="测试原生 Bedrock 模型")
    pb.add_argument("--filter", nargs="*"); pb.add_argument("--prompt", default="hi")
    pm = sub.add_parser("mantle", help="测试 Bedrock Mantle 模型")
    pm.add_argument("--filter", nargs="*"); pm.add_argument("--prompt", default="hi")
    pt = sub.add_parser("tickets", help="读取 AWS Support 工单")
    pt.add_argument("--filter", nargs="*"); pt.add_argument("--full", action="store_true")
    pa = sub.add_parser("all", help="auth → bedrock → mantle")
    pa.add_argument("--filter", nargs="*"); pa.add_argument("--prompt", default="hi")
    args = p.parse_args()

    if args.cmd == "auth":
        cmd_auth()
    elif args.cmd == "bedrock":
        cmd_bedrock(getattr(args, "filter", None), args.prompt)
    elif args.cmd == "mantle":
        cmd_mantle(getattr(args, "filter", None), args.prompt)
    elif args.cmd == "tickets":
        cmd_tickets(getattr(args, "filter", None), getattr(args, "full", False))
    elif args.cmd == "all":
        if not cmd_auth():
            return
        cmd_bedrock(getattr(args, "filter", None), args.prompt)
        cmd_mantle(getattr(args, "filter", None), args.prompt)


if __name__ == "__main__":
    main()
