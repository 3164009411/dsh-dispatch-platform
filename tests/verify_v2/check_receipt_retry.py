#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C4 判据：receipt 的重派链 / 状态枚举 / deadline。

用临时台账，不碰 `tools/dispatch/ledger.jsonl` 真文件：
  R1  原条目 → 追加 <id>-R1（parent_id、RETRY-SENT），原条目状态不变
  R2  再追加 <id>-R2
  ESC 第三次 → 追加 <id>-ESC（ESCALATED）并返回 4
  MARK 新状态 RETRY-SENT 可写；非法状态被 argparse 拒
  DEADLINE wait_file 到期返回 deadline
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "dispatch"))

import receipt  # noqa: E402


def entries():
    return receipt._read_ledger()


def by_id(eid):
    return next((e for e in entries() if e.get("id") == eid), None)


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="receipt-c4-")
    old = receipt.LEDGER_PATH
    receipt.LEDGER_PATH = os.path.join(tmpdir, "ledger.jsonl")
    bad = 0
    try:
        receipt.ledger_append({
            "id": "T-1", "ts": "2026-10-29 12:00:00", "platform": "qwen", "port": 9555,
            "task": "测试任务", "marker": "TASK-END", "expect_file": None,
            "status": "SENT", "note": "",
        })
        original_before = by_id("T-1")

        rc = receipt.main(["retry", "--id", "T-1"])
        r1 = by_id("T-1-R1")
        ok1 = rc == 0 and r1 and r1["parent_id"] == "T-1" and r1["status"] == "RETRY-SENT"
        ok1 = ok1 and by_id("T-1") == original_before
        print(f"[{'OK' if ok1 else 'BAD'}] R1 rc={rc} r1={r1 and r1['status']} 原条目未改写={by_id('T-1') == original_before}")
        bad += 0 if ok1 else 1

        rc = receipt.main(["retry", "--id", "T-1"])
        r2 = by_id("T-1-R2")
        ok2 = rc == 0 and r2 and r2["parent_id"] == "T-1"
        print(f"[{'OK' if ok2 else 'BAD'}] R2 rc={rc} r2={r2 and r2['status']}")
        bad += 0 if ok2 else 1

        rc = receipt.main(["retry", "--id", "T-1"])
        esc = by_id("T-1-ESC")
        ok3 = rc == 4 and esc and esc["status"] == "ESCALATED" and esc["parent_id"] == "T-1"
        print(f"[{'OK' if ok3 else 'BAD'}] ESC rc={rc} esc={esc and esc['status']}")
        bad += 0 if ok3 else 1

        receipt.main(["mark", "--id", "T-1-R1", "--status", "RETRY-SENT", "--note", "ok"])
        ok4 = by_id("T-1-R1")["status"] == "RETRY-SENT"
        print(f"[{'OK' if ok4 else 'BAD'}] MARK RETRY-SENT")
        bad += 0 if ok4 else 1

        rejected = False
        try:
            receipt.main(["mark", "--id", "T-1", "--status", "BOGUS"])
        except SystemExit as e:
            rejected = e.code != 0
        print(f"[{'OK' if rejected else 'BAD'}] MARK 非法状态被拒")
        bad += 0 if rejected else 1

        r = receipt.wait_file(os.path.join(tmpdir, "nope.md"), timeout=5,
                              deadline_at=time.time() + 1.0)
        ok5 = r.get("deadline") is True
        print(f"[{'OK' if ok5 else 'BAD'}] DEADLINE {r}")
        bad += 0 if ok5 else 1
    finally:
        receipt.LEDGER_PATH = old
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("RECEIPT_RETRY", "OK" if not bad else "BAD", f"{6 - bad}/6")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
