#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C3 判据：`wait_reply` 必须能区分 完成 / 挂死 / DEADLINE。

用假 Page 注入活动序列，不碰真实平台：
  DONE     文本稳定且增长、无思考指示器
  HANG     连续无活动（文本/指示器/滚动都不动）超过 timeout
  DEADLINE 总时长到达 deadline（即使一直在动）

跑法：python check_dispatch_wait.py   （约 8s，0/1）
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "cdp"))

import dispatch  # noqa: E402


class FakePage:
    def __init__(self, sigs, length=100):
        self.sigs = list(sigs)
        self.i = 0
        self.length = length

    async def ev(self, expr):
        return self.length

    async def ev_json(self, expr):
        sig = self.sigs[min(self.i, len(self.sigs) - 1)]
        self.i += 1
        return sig


def sig(t, thinking=False, st=0):
    return {"t": t, "thinking": thinking, "st": st}


async def scenario_done():
    page = FakePage([sig(100), sig(110), sig(110), sig(110), sig(110)], length=110)
    status, delta, secs = await dispatch.wait_reply(page, 100, timeout=5, port=0)
    return status == "DONE" and delta == 10, (status, delta, secs)


async def scenario_hang():
    page = FakePage([sig(100)], length=100)
    status, delta, secs = await dispatch.wait_reply(page, 100, timeout=1.5, port=0)
    return status == "HANG", (status, delta, secs)


async def scenario_deadline():
    page = FakePage([sig(100), sig(101), sig(102), sig(103), sig(104), sig(105)], length=105)
    status, delta, secs = await dispatch.wait_reply(page, 100, timeout=60, port=0, deadline=2.2)
    return status == "DEADLINE", (status, delta, secs)


async def main():
    bad = 0
    for name, fn in (("DONE", scenario_done), ("HANG", scenario_hang), ("DEADLINE", scenario_deadline)):
        ok, detail = await fn()
        print(f"[{'OK' if ok else 'BAD'}] {name:<10}{detail}")
        bad += 0 if ok else 1
    print("DISPATCH_WAIT", "OK" if not bad else "BAD", f"{3 - bad}/3")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
