#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""receipt.py — 派发回执判定 + 投递台账（ledger.jsonl）。

★ 只认三种硬信号（DSH 实测 36 条通知只有 8 条真进去 ⇒ "对方说做完了"不算回执）：
  1. `--file <路径>`   指定产物文件出现
  2. `--marker <文本>` 结束标记出现在页面里，**且其后页面文本长度连续 2 轮不再变化**
     （防"自己的消息气泡一渲染就误判回复"——同 dispatch.py 的教训）
  3. `confirm` 子命令  人工确认（原样记台账，注明"人工确认"）

★ 台账 ledger.jsonl（append-mostly；mark 用临时文件+os.replace 原子重写）：
  每条 = {id, ts, platform, port, task, marker, expect_file, status, note[, parent_id]}
  status ∈ SENT / REPLIED / TIMEOUT / FAILED / RETRY-SENT / ESCALATED / DEADLINE
  重派：append 新条目（id=<原id>-R1、parent_id=<原id>、RETRY-SENT），原条目永不改写；
        超过 max_retries（默认 2）追加 <原id>-ESC（ESCALATED）升级给人。
  没有台账的重派 = 靠记忆，等于没有重派。

用法：
  python receipt.py log --platform qwen --port 9555 --task "写周报" --marker "周报已完成"
  python receipt.py wait --id QWEN-20261028-153000 [--timeout 120] [--deadline 600]
  python receipt.py wait --marker "文本" --port 9555 --timeout 60       # 脱台账单次判
  python receipt.py wait --file out\report.md --timeout 300
  python receipt.py retry --id QWEN-... [--max-retries 2]               # 补发「继续」
  python receipt.py confirm --id QWEN-... --note "人看过，确实做完了"
  python receipt.py mark --id QWEN-... --status FAILED --note "超时两次"
  python receipt.py list [--status SENT]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

try:
    import websockets
except ImportError:
    sys.stderr.write("MISSING: 需要 websockets（pip install websockets）\n")
    sys.exit(2)

try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None
try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

_HERE = os.path.dirname(os.path.abspath(__file__))
# 允许测试/演练用独立台账，避免污染生产 ledger。
LEDGER_PATH = os.environ.get("RECEIPT_LEDGER") or os.path.join(_HERE, "ledger.jsonl")

# 状态枚举（v1.2/C4）：SENT → REPLIED / TIMEOUT / FAILED；重派链 RETRY-SENT；升级 ESCALATED
STATUSES = ("SENT", "REPLIED", "TIMEOUT", "FAILED", "RETRY-SENT", "ESCALATED", "DEADLINE")

sys.path.insert(0, os.path.dirname(_HERE))
from cdp.dispatch import pick_page, Page  # noqa: E402


# ---------------- 台账 ----------------

def _read_ledger() -> list:
    if not os.path.exists(LEDGER_PATH):
        return []
    out = []
    with open(LEDGER_PATH, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # 畸形行不静默丢（E42 反检教训）：报出来带行号
                sys.stderr.write("WARN: ledger.jsonl 第 %d 行不是合法 JSON，已跳过\n" % ln)
                continue
            if not isinstance(obj, dict):
                sys.stderr.write("WARN: ledger.jsonl 第 %d 行不是 JSON 对象，已跳过\n" % ln)
                continue
            out.append(obj)
    return out


def _write_ledger(entries: list) -> None:
    tmp = "%s.tmp.%d" % (LEDGER_PATH, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, LEDGER_PATH)


class _LedgerLock:
    """台账跨进程互斥。

    只把 ledger_append 改成 "a" 模式并不够：Windows CRT 的追加模式是
    "lseek 到 EOF 再写"（非原子），两个进程并发 append 会互相覆盖
    （实测 60 条只剩 39 条）。用独立锁文件串行化读-改-写。
    """

    def __init__(self, path: str):
        self._lock_path = path + ".lock"
        self._fh = None

    def __enter__(self):
        self._fh = open(self._lock_path, "a+b")
        deadline = time.time() + 10.0
        while True:
            try:
                if msvcrt is not None:
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                elif fcntl is not None:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() >= deadline:
                    self._fh.close()
                    raise RuntimeError("台账锁 10s 未释放：%s" % self._lock_path)
                time.sleep(0.05)

    def __exit__(self, *a):
        try:
            if msvcrt is not None:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
        return False


def _append_line(rec: dict) -> str:
    with open(LEDGER_PATH, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec["id"]


def ledger_append(rec: dict) -> str:
    """真追加 + 跨进程锁：不再"读全表→整表重写"，
    避免两个并发投递互相覆盖丢条目（台账是判决证据，丢一条就是丢一个事实）。"""
    with _LedgerLock(LEDGER_PATH):
        return _append_line(rec)


def ledger_update(eid: str, **kw) -> None:
    with _LedgerLock(LEDGER_PATH):
        entries = _read_ledger()
        for e in entries:
            if e.get("id") == eid:
                e.update(kw)
                _write_ledger(entries)
                return
    raise RuntimeError("台账里没有 id=%r 的条目" % eid)


def new_id(platform: str, taken=()) -> str:
    """id 必须唯一：同一平台同一秒两次 log 曾生成重复 id，
    wait/confirm 只命中第一条 ⇒ 第二条永远等不到。冲突时追加 -2/-3…（正则兼容）。"""
    base = "%s-%s" % (platform.upper(), time.strftime("%Y%m%d-%H%M%S"))
    if base not in taken:
        return base
    n = 2
    while "%s-%d" % (base, n) in taken:
        n += 1
    return "%s-%d" % (base, n)


# ---------------- 回执判定 ----------------

async def wait_marker(port: int, marker: str, timeout: int, deadline_at=None) -> dict:
    """连 CDP 轮询页面文本：marker 出现且其后长度连续 2 轮稳定 ⇒ 回执成立。
    只报告命中与否，不打印页面内容。
    总时长：给了 deadline_at 就等 deadline（README §4 的 300/900 组合），否则等 timeout。"""
    t, idx, total, why = pick_page(port)
    async with websockets.connect(t["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as ws:
        p = Page(ws, t.get("url", ""))
        t0 = time.time()
        end = deadline_at if deadline_at is not None else t0 + timeout
        last_len = None
        stable = 0
        while time.time() < end:
            text = await p.ev("(document.body.innerText||'')")
            if marker in text:
                if last_len is not None and len(text) == last_len:
                    stable += 1
                else:
                    stable = 0
                last_len = len(text)
                if stable >= 2:  # 连续 2 轮（间隔 1s）长度不变 ⇒ 生成已结束
                    return {"ok": True, "how": "marker 命中且页面已稳定",
                            "secs": round(time.time() - t0, 1)}
            else:
                stable = 0
                last_len = None
            await asyncio.sleep(1.0)
        if deadline_at is not None and time.time() >= deadline_at:
            return {"ok": False, "how": "DEADLINE（总时长封顶）",
                    "secs": round(time.time() - t0, 1), "deadline": True}
        return {"ok": False, "how": "超时未见 marker 或页面持续变化",
                "secs": round(time.time() - t0, 1)}


def _since_epoch(rec: dict):
    """台账 ts（派发时刻）→ epoch；用于把"同名旧产物"挡在回执之外。"""
    ts = rec.get("ts")
    if not ts:
        return None
    try:
        return time.mktime(time.strptime(str(ts), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return None


def wait_file(path: str, timeout: int, deadline_at=None, since=None) -> dict:
    t0 = time.time()
    end = deadline_at if deadline_at is not None else t0 + timeout
    stale = False
    while time.time() < end:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            if since is None or os.path.getmtime(path) >= since - 2.0:
                return {"ok": True, "how": "产物文件已出现（%d B）" % os.path.getsize(path),
                        "secs": round(time.time() - t0, 1)}
            stale = True  # 同名旧文件早于派发时刻 ⇒ 不是这次派发的证据
        time.sleep(1.0)
    if deadline_at is not None and time.time() >= deadline_at:
        return {"ok": False, "how": "DEADLINE（总时长封顶）",
                "secs": round(time.time() - t0, 1), "deadline": True}
    how = "超时未见新产物（同名旧文件早于派发时间）" if stale else "超时未见产物文件"
    return {"ok": False, "how": how, "secs": round(time.time() - t0, 1)}


def _dir_has_files(path: str, since=None) -> bool:
    if not os.path.isdir(path):
        return False
    for _root, _dirs, files in os.walk(path):
        for f in files:
            if f.startswith("."):
                continue
            if since is None:
                return True
            try:
                if os.path.getmtime(os.path.join(_root, f)) >= since - 2.0:
                    return True
            except OSError:
                continue
    return False


def wait_dir(path: str, timeout: int, deadline_at=None, since=None) -> dict:
    t0 = time.time()
    end = deadline_at if deadline_at is not None else t0 + timeout
    while time.time() < end:
        if _dir_has_files(path, since):
            return {"ok": True, "how": "产物目录已有文件", "secs": round(time.time() - t0, 1)}
        time.sleep(1.0)
    if deadline_at is not None and time.time() >= deadline_at:
        return {"ok": False, "how": "DEADLINE（总时长封顶）",
                "secs": round(time.time() - t0, 1), "deadline": True}
    return {"ok": False, "how": "超时未见产物目录文件", "secs": round(time.time() - t0, 1)}


def artifact_check(rec: dict):
    """返回 (ok, why)。confirm/wait 前必须先过这一关，防“对方说完成”假回执。
    since = 台账派发时刻：同名旧产物（mtime 更早）不算这次派发的产物。"""
    since = _since_epoch(rec)
    path = rec.get("expect_file")
    if path:
        if not (os.path.isfile(path) and os.path.getsize(path) > 0):
            return False, "expect_file 不存在或为空：%s" % path
        if since is not None and os.path.getmtime(path) < since - 2.0:
            return False, "expect_file 早于派发时间（陈旧产物，不是这次派发的证据）：%s" % path
    d = rec.get("expect_dir")
    if d and not _dir_has_files(d, since):
        return False, "expect_dir 不存在或无派发后新文件：%s" % d
    return True, ""


def do_wait(args) -> int:
    """按台账条目或直接参数判回执；判完回写台账。"""
    marker, expect_file, expect_dir, eid = args.marker, args.file, getattr(args, "dir", None), args.id
    port, platform = args.port, args.platform
    if eid:
        entries = _read_ledger()
        rec = next((e for e in entries if e.get("id") == eid), None)
        if not rec:
            print("RESULT: FAIL —— 台账里没有 id=%r" % eid)
            return 1
        marker = rec.get("marker")
        expect_file = rec.get("expect_file")
        expect_dir = rec.get("expect_dir")
        port = rec.get("port") or port
        platform = rec.get("platform", platform)
        print("[台账] %s：%s（状态 %s）" % (eid, rec.get("task", ""), rec.get("status")))
    since = _since_epoch(rec) if eid else None
    if not marker and not expect_file and not expect_dir:
        print("RESULT: FAIL —— 台账条目里既无 marker 也无产物路径，判不了")
        return 1
    if marker and not expect_file and not expect_dir and not port:
        # 历史台账里存在 port=null 的条目（log 时未给 --port）：直接 %d 会 TypeError 崩掉，
        # 这里必须 fail-loud 给出下一步命令，而不是抛栈。
        print("RESULT: FAIL —— 该条目没有 port，marker 判定需连 CDP；请补 --port（如 9555/9333）")
        return 1

    deadline_at = (time.time() + args.deadline) if getattr(args, "deadline", None) else None
    if expect_file:
        r = wait_file(expect_file, args.timeout, deadline_at, since)
    elif expect_dir:
        r = wait_dir(expect_dir, args.timeout, deadline_at, since)
    else:
        r = asyncio.run(wait_marker(port, marker, args.timeout, deadline_at))
    print("[回执] %s（%s，用时 %ss）" % ("成立" if r["ok"] else "**不成立**", r["how"], r["secs"]))
    status = "DEADLINE" if r.get("deadline") else ("REPLIED" if r["ok"] else "TIMEOUT")
    if eid:
        upd = {"status": status, "receipt_how": r["how"]}
        if r["ok"]:
            # 失败也写 replied_at 会让字段语义失真（生产台账里已出现 TIMEOUT 带 replied_at）。
            upd["replied_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        ledger_update(eid, **upd)
        print("[台账] %s → %s" % (eid, status))
    print("RESULT: %s" % ("PASS" if r["ok"] else status))
    if r["ok"]:
        return 0
    return 4 if r.get("deadline") else 3


def do_retry(args) -> int:
    """重派：append-only。原条目永不改写；新条目 parent_id 指向原 id。

    链长 = 原条目 + 已有重派数；超过 max_retries（默认 2）⇒ 追加 ESCALATED，升级给人。
    """
    entries = _read_ledger()
    orig = next((e for e in entries if e.get("id") == args.id), None)
    if not orig:
        print("RESULT: FAIL —— 台账里没有 id=%r" % args.id)
        return 1
    chain = [e for e in entries if e.get("parent_id") == orig["id"]]
    n = len(chain) + 1
    if n > args.max_retries:
        eid = orig["id"] + "-ESC"
        if any(e.get("id") == eid for e in entries):
            print("RESULT: FAIL —— 已存在升级条目 %s" % eid)
            return 1
        rec = {
            "id": eid,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "platform": orig.get("platform"),
            "port": orig.get("port"),
            "task": orig.get("task"),
            "marker": orig.get("marker"),
            "expect_file": orig.get("expect_file"),
            "expect_dir": orig.get("expect_dir"),
            "status": "ESCALATED",
            "note": args.note or "超过 max_retries=%d，升级给人" % args.max_retries,
            "parent_id": orig["id"],
        }
        ledger_append(rec)
        print("[台账] %s → ESCALATED（重派 %d 次，超过 max_retries=%d）"
              % (orig["id"], len(chain), args.max_retries))
        print("RESULT: ESCALATED")
        return 4

    eid = "%s-R%d" % (orig["id"], n)
    if any(e.get("id") == eid for e in entries):
        print("RESULT: FAIL —— 已存在重派条目 %s" % eid)
        return 1
    rec = {
        "id": eid,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": orig.get("platform"),
        "port": orig.get("port"),
        "task": orig.get("task"),
        "marker": orig.get("marker"),
        "expect_file": orig.get("expect_file"),
        "expect_dir": orig.get("expect_dir"),
        "status": "RETRY-SENT",
        "note": args.note,
        "parent_id": orig["id"],
    }
    ledger_append(rec)
    print("[台账] %s → 新条目 %s（parent_id=%s，RETRY-SENT）" % (orig["id"], eid, orig["id"]))
    print("RESULT: PASS")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description="派发回执判定 + 投递台账")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("log", help="记一条投递（status=SENT）")
    sp.add_argument("--platform", required=True)
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--task", required=True, help="任务摘要（不用全文）")
    sp.add_argument("--marker", default=None, help="期望出现在回复里的结束标记")
    sp.add_argument("--expect-file", default=None, help="期望出现的产物文件路径")
    sp.add_argument("--expect-dir", default=None, help="期望出现文件的产物目录（递归）")

    sp = sub.add_parser("wait", help="等回执（file/dir 或 marker）")
    sp.add_argument("--id", default=None, help="按台账条目判")
    sp.add_argument("--marker", default=None)
    sp.add_argument("--file", dest="file", default=None)
    sp.add_argument("--dir", dest="dir", default=None, help="等目录内出现任意文件")
    sp.add_argument("--platform", default=None)
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--timeout", type=int, default=120,
                    help="未给 --deadline 时的总等待秒数（给了 --deadline 就等 deadline）")
    sp.add_argument("--deadline", type=int, default=None,
                    help="总等待时长秒（给值即按它等；到点记 DEADLINE）")

    sp = sub.add_parser("retry", help="登记重派（append-only，原条目不改写；实际补发由调用方执行）")
    sp.add_argument("--id", required=True)
    sp.add_argument("--max-retries", type=int, default=2)
    sp.add_argument("--note", default="")

    sp = sub.add_parser("confirm", help="人工确认回执（默认先校验产物）")
    sp.add_argument("--id", required=True)
    sp.add_argument("--note", default="人工确认")
    sp.add_argument("--allow-no-artifact", action="store_true",
                    help="仅 marker 回执且人工已核实页面时使用；无产物不得默认放行")

    sp = sub.add_parser("mark", help="手工改状态")
    sp.add_argument("--id", required=True)
    sp.add_argument("--status", required=True, choices=list(STATUSES))
    sp.add_argument("--note", default="")

    sp = sub.add_parser("list", help="看台账")
    sp.add_argument("--status", default=None, help="只看某状态")
    sp.add_argument("--last", type=int, default=20, help="最多显示条数")

    a = ap.parse_args(argv)

    if a.cmd == "log":
        if not a.marker and not a.expect_file and not a.expect_dir:
            ap.error("log 必须带 --marker / --expect-file / --expect-dir（没有硬信号的投递=扔进黑洞）")
        with _LedgerLock(LEDGER_PATH):
            rec = {"id": new_id(a.platform, {e.get("id") for e in _read_ledger()}),
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "platform": a.platform, "port": a.port, "task": a.task,
                   "marker": a.marker, "expect_file": a.expect_file,
                   "expect_dir": a.expect_dir,
                   "status": "SENT", "note": ""}
            _append_line(rec)
        print("[台账] 已记 %s（%s → %s，marker=%r）"
              % (rec["id"], a.platform, a.task[:40], a.marker))
        print("RESULT: PASS")
        return 0

    if a.cmd == "wait":
        return do_wait(a)

    if a.cmd == "retry":
        return do_retry(a)

    if a.cmd == "confirm":
        entries = _read_ledger()
        rec = next((e for e in entries if e.get("id") == a.id), None)
        if not rec:
            print("RESULT: FAIL —— 台账里没有 id=%r" % a.id)
            return 1
        has_artifact = bool(rec.get("expect_file") or rec.get("expect_dir"))
        if not has_artifact and not a.allow_no_artifact:
            print("RESULT: FAIL —— 台账没有产物路径；无产物不得确认 REPLIED。"
                  "如仅 marker 且已人工核实，请显式加 --allow-no-artifact")
            return 3
        ok, why = artifact_check(rec)
        if not ok:
            print("[产物校验] FAIL —— %s" % why)
            print("RESULT: FAIL —— 未过产物校验，台账保持 %s" % rec.get("status"))
            return 3
        note = a.note if has_artifact else (a.note + "（仅 marker，无产物路径）")
        ledger_update(a.id, status="REPLIED", note=note,
                      replied_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                      artifact_verified=has_artifact)
        print("[台账] %s → REPLIED（%s）" % (a.id, note))
        print("RESULT: PASS")
        return 0

    if a.cmd == "mark":
        try:
            ledger_update(a.id, status=a.status, note=a.note)
        except RuntimeError:
            print("RESULT: FAIL —— 台账里没有 id=%r" % a.id)
            return 1
        print("[台账] %s → %s" % (a.id, a.status))
        print("RESULT: PASS")
        return 0

    if a.cmd == "list":
        entries = _read_ledger()
        if a.status:
            entries = [e for e in entries if e.get("status") == a.status]
        print("台账共 %d 条（显示最近 %d）：" % (len(entries), a.last))
        shown = entries[-a.last:] if a.last > 0 else []
        for e in shown:
            print("  %s │ %s │ %-7s │ %s %s"
                  % (e.get("id"), e.get("ts"), e.get("status"),
                     (e.get("task") or "")[:36], ("│ " + e["note"]) if e.get("note") else ""))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
