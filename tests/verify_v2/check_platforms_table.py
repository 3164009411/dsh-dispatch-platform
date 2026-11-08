#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""平台能力表判据 —— 守 `tools/dispatch/platforms.json` 的结构与路由约束。

判据（每条都会失败）：
  [PLATFORMS_MISSING]   平台集合与 platforms.PLATFORM_IDS 不一致
  [PLATFORMS_ROUTABLE]  unprobed 平台被当成可路由
  [PLATFORMS_CARD]      任务卡缺字段
  schema/端口/枚举错误由 platforms.validate 直接返回错误码。

用法：
  python check_platforms_table.py
  python check_platforms_table.py --json
  python check_platforms_table.py --selftest
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "dispatch"))

import platforms as P  # noqa: E402


def _codes(data: dict) -> list:
    try:
        P.validate(data)
    except P.PlatformTableError as e:
        return [e.code]
    return []


def check(data: dict) -> list:
    problems = _codes(data)
    if problems:
        return problems
    plats = data["platforms"]
    if set(plats) != set(P.PLATFORM_IDS):
        problems.append("PLATFORMS_MISSING")
    for _pid, p in plats.items():
        if p.get("status") == "unprobed" and P.routable(p):
            problems.append("PLATFORMS_ROUTABLE")
    try:
        card = P.make_task_card(plats[P.PLATFORM_IDS[0]], "CARD-1", "review",
                                marker_suffix="deadbeef", deadline=60,
                                background_files=["a.md"], acceptance=["done"])
    except P.PlatformTableError:
        problems.append("PLATFORMS_CARD")
    else:
        need = {"task_id", "type", "confirm_mode", "deadline", "marker_suffix",
                "background_files", "acceptance", "work_mode", "mode_switch_state",
                "model", "model_switch_state"}
        if set(card) != need or card["marker_suffix"] != "deadbeef":
            problems.append("PLATFORMS_CARD")
    return problems


def selftest() -> int:
    base = P.load()
    cases = []

    def case(name, mutate, expect):
        d = copy.deepcopy(base)
        mutate(d)
        cases.append((name, expect, _codes(d)))

    case("valid", lambda d: None, None)
    case("missing_field", lambda d: d["platforms"]["qwen"].pop("read_local_files"), "MISSING_FIELD")
    case("port_dup", lambda d: d["platforms"]["qwen"].__setitem__("port", d["platforms"]["doubao"]["port"]), "PORT_DUP")
    case("bad_status", lambda d: d["platforms"]["qwen"].__setitem__("status", "bogus"), "INVALID_STATUS")
    case("bad_port", lambda d: d["platforms"]["qwen"].__setitem__("port", "9555"), "INVALID_PORT")
    bad = 0
    for name, expect, codes in cases:
        ok = (codes == []) if expect is None else (expect in codes)
        print("[%s] %-14s %s" % ("OK" if ok else "BAD", name, codes))
        bad += 0 if ok else 1
    print("PLATFORMS_SELFTEST", "OK" if not bad else "BAD", "%d/%d" % (len(cases) - bad, len(cases)))
    return 0 if not bad else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    try:
        data = P.load()
    except P.PlatformTableError as e:
        print("PLATFORMS_TABLE FAIL %s: %s" % (e.code, e.message))
        return 1
    problems = check(data)
    if problems:
        print("PLATFORMS_TABLE FAIL " + ",".join(sorted(set(problems))))
        return 1
    if args.json:
        print(json.dumps({"platforms": sorted(data["platforms"]), "ok": True}, ensure_ascii=False))
    else:
        print("PLATFORMS_TABLE OK %d platforms" % len(data["platforms"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
