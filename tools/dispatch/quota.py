#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""quota.py — 平台额度自动探测（试点：千问办公），结果落 quota.json。

★ 原理（2026-10-28 千问实测）：
  点击左下角「个人免费版」徽标 → 弹出个人菜单 → 文本含「积分余额 <数字>」
  → 读到数字后 Escape 收起菜单（实测 body 长度还原，无残留）。
  全程不输入、不改设置、不切会话；唯一的动作是"开菜单 + Esc 关菜单"。

★ 判据纪律：
  - 写入 quota.json 前必须真的读到数字（正则 `积分余额\s*([\d.]+)`）；
    没读到就是 FAIL，**不许把"点了菜单"当"读到了额度"**（报成功≠打对地方）。
  - 写 quota.json 用临时文件 + os.replace 原子替换。
  - 写完回读校验：读回来的值必须等于刚写的值。

用法：
  python quota.py --platform qwen --port 9555              # 探测并落盘
  python quota.py --platform qwen --port 9555 --check 100  # 探测并判断额度是否 >= 100
  python quota.py --show                                    # 只看 quota.json 现状
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.request

try:
    import websockets
except ImportError:
    sys.stderr.write("MISSING: 需要 websockets（pip install websockets）\n")
    sys.exit(2)

_HERE = os.path.dirname(os.path.abspath(__file__))
QUOTA_PATH = os.path.join(_HERE, "quota.json")

sys.path.insert(0, os.path.dirname(_HERE))  # tools/ 下找 cdp/dispatch.py
from cdp.dispatch import pick_page, Page  # noqa: E402

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ---- 平台注册表：新平台在这里加一条（探测函数自己写） ----
# qwen: 点击「xx版」徽标 → 读「积分余额」
QWEN_BADGE_JS = """
(() => {
  // 找左下角的"个人xx版"徽标：叶子元素、文本以"版"结尾、在窗口下部
  const all = Array.from(document.querySelectorAll('*')).reverse();
  const el = all.find(e => {
    if (e.children.length > 2) return false;
    const t = (e.innerText || '').trim();
    if (!/^个人.*版$|^专业.*版$|^企业.*版$/.test(t)) return false;
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.top > window.innerHeight * 0.5;
  });
  if (!el) return JSON.stringify({found:false});
  const r = el.getBoundingClientRect();
  return JSON.stringify({found:true, tag:el.tagName,
    x:Math.round(r.left + r.width/2), y:Math.round(r.top + r.height/2)});
})()
"""
QWEN_BALANCE_RE = re.compile(r"积分余额\s*\n?\s*([0-9][0-9,]*(?:\.[0-9]+)?)")


async def probe_qwen(port: int) -> dict:
    """千问探测：开个人菜单 → 读积分余额 → Esc 收起。返回探测结果 dict。"""
    t, idx, total, why = pick_page(port)
    async with websockets.connect(t["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as ws:
        p = Page(ws, t.get("url", ""))

        async def esc():
            for tt in ("keyDown", "keyUp"):
                await p.send("Input.dispatchKeyEvent", type=tt, key="Escape",
                             code="Escape", windowsVirtualKeyCode=27, nativeVirtualKeyCode=27)

        # 菜单可能已开着：先看有没有"积分余额"
        m = QWEN_BALANCE_RE.search(await p.ev("(document.body.innerText||'')"))
        if not m:
            loc = json.loads(await p.ev(QWEN_BADGE_JS))
            if not loc.get("found"):
                raise RuntimeError("找不到「个人xx版」徽标（菜单入口变了？）")
            await p.send("Input.dispatchMouseEvent", type="mouseMoved", x=loc["x"], y=loc["y"])
            await asyncio.sleep(0.15)
            await p.send("Input.dispatchMouseEvent", type="mousePressed", x=loc["x"], y=loc["y"],
                         button="left", buttons=1, clickCount=1)
            await asyncio.sleep(0.08)
            await p.send("Input.dispatchMouseEvent", type="mouseReleased", x=loc["x"], y=loc["y"],
                         button="left", buttons=0, clickCount=1)
            await asyncio.sleep(1.5)
            m = QWEN_BALANCE_RE.search(await p.ev("(document.body.innerText||'')"))
            # 无论读没读到都收起菜单
            await esc()
            await asyncio.sleep(0.4)
        if not m:
            raise RuntimeError("菜单里没有「积分余额」（文案变了？把实测文本发回来更新正则）")
        val = float(m.group(1).replace(",", ""))
        return {"quota": val, "unit": "积分",
                "source": "CDP-DOM：点击左下角版本徽标→个人菜单「积分余额」"}


PROBERS = {"qwen": probe_qwen}


def load_quota() -> dict:
    if not os.path.exists(QUOTA_PATH):
        return {}
    with open(QUOTA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_quota(data: dict) -> None:
    tmp = QUOTA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, QUOTA_PATH)
    # 回读校验：没报错 ≠ 写进去了
    with open(QUOTA_PATH, "r", encoding="utf-8") as f:
        if json.load(f) != data:
            raise RuntimeError("quota.json 回读不一致，写入失败")


def main(argv):
    ap = argparse.ArgumentParser(description="平台额度自动探测（落 quota.json）")
    ap.add_argument("--platform", default="qwen", help="平台键（现支持 qwen）")
    ap.add_argument("--port", type=int, default=9555, help="该平台 CDP 调试端口")
    ap.add_argument("--check", type=float, default=None, help="探测后判断额度是否 >= 该值")
    ap.add_argument("--show", action="store_true", help="只看 quota.json 现状")
    a = ap.parse_args(argv)

    if a.show:
        print(json.dumps(load_quota(), ensure_ascii=False, indent=2))
        return 0
    if a.platform not in PROBERS:
        print("RESULT: FAIL —— 平台 %r 还没有探测实现（现支持：%s）"
              % (a.platform, ", ".join(PROBERS)))
        return 2
    try:
        r = PROBERS[a.platform](a.port)
        r = asyncio.run(r) if asyncio.iscoroutine(r) else r
    except Exception as e:
        print("RESULT: FAIL —— %s: %s" % (type(e).__name__, e))
        return 2

    data = load_quota()
    data[a.platform] = {
        "platform": a.platform, "port": a.port,
        "quota": r["quota"], "unit": r["unit"],
        "detected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": r["source"],
    }
    save_quota(data)
    print("[探测] %s 积分余额 = %s（已写入 quota.json 并回读一致）" % (a.platform, r["quota"]))
    if a.check is not None:
        ok = r["quota"] >= a.check
        print("[判据] 额度 %s >= %s ？%s" % (r["quota"], a.check, "是" if ok else "**否**"))
        print("RESULT: %s" % ("PASS" if ok else "BELOW"))
        return 0 if ok else 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
