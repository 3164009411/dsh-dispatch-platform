#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""doctor.py — 外派通道自检（跨会话第一件事）。

为什么需要它：
  端口关闭 / 应用没启动 ≠ 平台不能外派。过去多次把“没拉起”误报成“通道不通”，
  然后把“对方回复了”误报成“产物已写”。本脚本只回答两件事：
    1) 每个平台现在是 CLOSED / NO_PAGE / WRONG_WINDOW / READY；
    2) 若不是 READY，下一条要执行的拉起/切换命令是什么。

用法：
  python tools/dispatch/doctor.py            # 人类可读
  python tools/dispatch/doctor.py --json     # 机读
  python tools/dispatch/doctor.py --self-test # 分类器自证（不连端口）

退出码：0 = 就绪口径通过（默认"全部 READY"；加 --any-ready 则"任一 READY"）；
        1 = 未通过（输出仍含下一步命令）；2 = 脚本/配置错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORMS_JSON = os.path.join(HERE, "platforms.json")
sys.path.insert(0, HERE)  # 同目录 platforms.py（唯一真源校验器），脚本/被 import 两种跑法都能找到

# 本机请求必须绕过系统代理：环境里的 HTTP(S)_PROXY 会劫持 127.0.0.1（dispatch.py 实测报 502），
# 否则代理开启时会把"应用在跑"误报成 CLOSED —— 与本工具存在的意义正好相反。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 拉起命令（跨会话固定入口；路径写在 .cmd 里，本文件不出现盘符，过 ABS_CODE 判据）
LAUNCH = {
    "qwen": r'tools\launchers\start-qwenwork-cdp.cmd',
    "doubao": r'tools\launchers\start-doubao-cdp.cmd',
    "dumate": r'tools\launchers\start-dumate-cdp.cmd',
    "trae": r'tools\launchers\start-trae-cdp.cmd',
    "workbuddy": r'tools\launchers\start-workbuddy-cdp.cmd',
}

# 页面期望：字符串表示 URL 需包含；None 表示任意 page。
EXPECT_PAGE = {
    "qwen": "windowId=main",
    "doubao": "doubao-launcher",
    "dumate": None,
    "trae": "solo-lite",          # 目标窗口页面片段
    "workbuddy": None,
}


def fetch_pages(port: int, timeout: float = 2.0):
    try:
        with _OPENER.open("http://127.0.0.1:%d/json" % port, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return [p for p in data if p.get("type") == "page"], None
    except Exception as e:
        return None, str(e)


def safe_page(url: str) -> str:
    """只留页面文件名；绝不输出 query/fragment（可能含 token/cookie）。"""
    try:
        path = urllib.parse.urlsplit(url or "").path
        name = os.path.basename(path.replace("\\", "/"))
        return name or "(page)"
    except Exception:
        return "(page)"


def classify(platform: str, pages):
    """纯函数：给 page 列表，返回 (status, detail)。"""
    if pages is None:
        return "CLOSED", "端口无响应（应用未启动/未开 CDP）"
    if not pages:
        return "NO_PAGE", "端口开着但没有 page"
    urls = [p.get("url") or "" for p in pages]
    want = EXPECT_PAGE.get(platform)
    if want and not any(want in u for u in urls):
        if platform == "trae" and any("workbench" in u for u in urls):
            return "WRONG_WINDOW", "只命中聊天栏页面；需切到目标窗口（页面片段匹配）"
        return "WRONG_WINDOW", "页面不含期望片段 %r" % want
    return "READY", "page 命中"


def classify_platform(platform: str, pages):
    """在 classify 之上补 fail-closed：能力表新增平台但 doctor 没登记期望页面/拉起命令时，
    不能因为 EXPECT_PAGE 取不到值就默认 READY（旧行为 = 新平台永远"就绪"，误报 ready）。"""
    if platform not in LAUNCH or platform not in EXPECT_PAGE:
        return "UNREGISTERED", "能力表平台未在 doctor 登记期望页面/拉起命令（补 EXPECT_PAGE/LAUNCH）"
    return classify(platform, pages)


def self_test() -> int:
    cases = [
        ("qwen", None, "CLOSED"),
        ("qwen", [], "NO_PAGE"),
        ("qwen", [{"url": "x/windowId=main"}], "READY"),
        ("qwen", [{"url": "x/other"}], "WRONG_WINDOW"),
        ("trae", [{"url": "x/workbench.html"}], "WRONG_WINDOW"),
        ("trae", [{"url": "x/solo-lite.html"}], "READY"),
        ("workbuddy", [{"url": "x/index.html"}], "READY"),
        ("newp", [{"url": "x/index.html"}], "UNREGISTERED"),
    ]
    bad = 0
    for platform, pages, want in cases:
        got, _ = classify_platform(platform, pages)
        ok = got == want
        print("[%s] %-9s want=%-12s got=%s" % ("PASS" if ok else "FAIL", platform, want, got))
        bad += 0 if ok else 1
    print("DOCTOR_SELFTEST", "PASS" if bad == 0 else "FAIL")
    return 0 if bad == 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="外派通道自检")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--any-ready", action="store_true",
                    help="至少一个平台 READY 即 exit 0（工具就绪口径；默认要求全部 READY）")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()

    try:
        import platforms as PT
        table = PT.load(PLATFORMS_JSON)["platforms"]  # 走唯一真源校验器，坏表 fail-loud
    except Exception as e:
        # 文档承诺：2 = 脚本/配置错误。裸异常会以 1 退出，与 README/退出码契约不符。
        sys.stderr.write("doctor: 能力表不可用（%s）：%s\n" % (PLATFORMS_JSON, e))
        return 2

    rows = []
    all_ready = True
    # 并行探测：五个平台串行时，三个 CLOSED 的 2s 超时叠加会超过 context_pack 的 5s probe 上限。
    from concurrent.futures import ThreadPoolExecutor
    items = list(table.items())
    with ThreadPoolExecutor(max_workers=max(1, len(items))) as pool:
        fetched = list(pool.map(lambda kv: fetch_pages(int(kv[1]["port"])), items))
    for (pid, cfg), (pages, err) in zip(items, fetched):
        status, detail = classify_platform(pid, pages)
        if status != "READY":
            all_ready = False
        row = {
            "platform": pid,
            "name": cfg.get("name", pid),
            "port": cfg.get("port"),
            "status": status,
            "detail": detail,
            "pages": [safe_page(p.get("url", "")) for p in (pages or [])],
            "next": LAUNCH.get(pid, ""),
        }
        rows.append(row)

    any_ready = any(r["status"] == "READY" for r in rows)
    effective_ready = any_ready if a.any_ready else all_ready
    if a.json:
        print(json.dumps({"ready": effective_ready, "all_ready": all_ready,
                          "platforms": rows}, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            print("%-10s %-12s %s" % (r["platform"], r["status"], r["detail"]))
            if r["status"] != "READY":
                print("           next: %s" % r["next"])
    return 0 if effective_ready else 1


if __name__ == "__main__":
    sys.exit(main())
