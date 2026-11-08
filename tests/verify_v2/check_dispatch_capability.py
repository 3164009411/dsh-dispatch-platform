#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""外派能力自检：doctor 分类/脱敏/代理/表校验 + receipt 产物强校验/台账唯一性/陈旧产物。

守住两类最容易退化的约束：
  1) 通道分类：端口关闭/页面选错/术语未登记/代理劫持 必须被分类出来，输出不得带 token；
  2) 回执判定：没产物、产物陈旧、id 重复、台账缺 port 时，不许记成 REPLIED，也不许抛栈。

跑法：python check_dispatch_capability.py（0/1）；约 8s，全部 tmp_path，不碰生产台账。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = sys.executable
DOCTOR = os.path.join(ROOT, "tools", "dispatch", "doctor.py")
RECEIPT = os.path.join(ROOT, "tools", "dispatch", "receipt.py")
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "dispatch"))

import doctor  # noqa: E402
import receipt  # noqa: E402
from cdp import dispatch as cdp_dispatch  # noqa: E402

# 子进程输出解码固定 UTF-8，但 Python 在中文 Windows 上写管道默认用 GBK：
# 不给子进程强制编码，下面的正则匹配中文台账行会假红（本判据自身曾因此稳定 FAIL）。
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")


def check(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  " + detail) if detail else ""))
    return ok


class _FakePageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps([{"type": "page", "url": "http://127.0.0.1/x/index.html"}]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpener:
    """替换 dispatch._OPENER，让 pick_page 不依赖真实 CDP 端口。"""

    def __init__(self, pages):
        self._pages = pages

    def open(self, url, timeout=None):
        return _Resp(self._pages)


def _run(cmd, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def main() -> int:
    ok = True

    # ---- A. doctor 自证 / 脱敏（原有） ----
    p = _run([PY, DOCTOR, "--self-test"], env=CHILD_ENV)
    ok &= check("doctor --self-test exit 0", p.returncode == 0, "rc=%d" % p.returncode)
    ok &= check("doctor self-test PASS 行", "DOCTOR_SELFTEST PASS" in (p.stdout or ""),
                (p.stdout or "").strip().splitlines()[-1] if (p.stdout or "").strip() else "")

    # 夹具用中性占位（不使用真实会话字段/值，避免公开仓库出现"像 token"的字面量）
    dirty = "file:///x/index.html?q=SHOULD_NOT_LEAK&p=SHOULD_NOT_LEAK2"
    safe = doctor.safe_page(dirty)
    ok &= check("doctor 页面脱敏只留文件名", safe == "index.html", safe)
    ok &= check("doctor 输出不含 query 内容", "SHOULD_NOT_LEAK" not in safe)

    # ---- B. 代理环境下仍要能探测（本地 127.0.0.1 不能被 HTTP_PROXY 劫持） ----
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakePageHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    old_proxy = {k: os.environ.get(k) for k in
                 ("HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")}
    try:
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"  # 死代理：真实代理会报 502
        os.environ["http_proxy"] = "http://127.0.0.1:9"
        os.environ["NO_PROXY"] = ""
        os.environ["no_proxy"] = ""
        pages, err = doctor.fetch_pages(port, timeout=1.0)
        ok &= check("doctor 本机探测绕过系统代理", pages is not None,
                    "err=%s" % (str(err)[:80] if err else ""))
    finally:
        for k, v in old_proxy.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        srv.shutdown()

    # ---- C. Trae 自动选页必须落到 Agent 窗口（solo-lite），落 workbench 要 fail-loud ----
    old_opener = cdp_dispatch._OPENER
    try:
        cdp_dispatch._OPENER = _FakeOpener([
            {"type": "page", "url": "https://x/workbench.html", "webSocketDebuggerUrl": "ws://1"},
            {"type": "page", "url": "https://x/solo-lite.html", "webSocketDebuggerUrl": "ws://2"},
        ])
        t, _i, _n, _why = cdp_dispatch.pick_page(9444)
        ok &= check("Trae 无 --match 时优先 solo-lite", "solo-lite" in (t.get("url") or ""),
                    t.get("url") or "")

        cdp_dispatch._OPENER = _FakeOpener([
            {"type": "page", "url": "https://x/workbench.html", "webSocketDebuggerUrl": "ws://1"},
        ])
        raised = False
        try:
            cdp_dispatch.pick_page(9444)
        except RuntimeError:
            raised = True
        ok &= check("Trae 只剩 workbench 时 fail-loud", raised)
    finally:
        cdp_dispatch._OPENER = old_opener

    # ---- D. 平台表 fail-closed：新平台未在 doctor 登记 ⇒ UNREGISTERED（旧行为 READY） ----
    status, _ = doctor.classify_platform("newp", [{"url": "x/index.html"}])
    ok &= check("未登记平台不得 fail-open 成 READY", status == "UNREGISTERED", status)

    # ---- E. 能力表坏表必须 exit 2（文档承诺，旧代码 traceback exit 1） ----
    td = tempfile.mkdtemp(prefix="dispatch-cap-")
    old_plats = doctor.PLATFORMS_JSON
    try:
        bad = os.path.join(td, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write('{"platforms": {"newp": {"name": "x", "port": 12345}}}')
        doctor.PLATFORMS_JSON = bad
        rc = doctor.main(["--json"])
        ok &= check("bad 能力表 doctor exit 2", rc == 2, "rc=%d" % rc)
    finally:
        doctor.PLATFORMS_JSON = old_plats

    # ---- F. receipt：CLI 端到端（log → 缺产物 confirm FAIL → 有产物 confirm PASS） ----
    td = tempfile.mkdtemp(prefix="dispatch-receipt-")
    ledger = os.path.join(td, "ledger.jsonl")
    artifact = os.path.join(td, "out.md")
    env = dict(CHILD_ENV, RECEIPT_LEDGER=ledger)
    r = _run([PY, RECEIPT, "log", "--platform", "test", "--task", "gate",
              "--expect-file", artifact], env=env)
    ok &= check("receipt log exit 0", r.returncode == 0, "rc=%d" % r.returncode)
    eid = ""
    m = re.search(r"\[台账\] 已记 (\S+?)（", r.stdout or "")
    if m:
        eid = m.group(1)
    ok &= check("receipt log 产出 id", bool(eid), eid)

    r = _run([PY, RECEIPT, "confirm", "--id", eid], env=env)
    ok &= check("缺产物 confirm 必须 FAIL", r.returncode == 3, "rc=%d" % r.returncode)
    with open(ledger, "r", encoding="utf-8") as f:
        rec = json.loads(f.read().splitlines()[-1])
    ok &= check("缺产物后台账仍为 SENT", rec.get("status") == "SENT", rec.get("status"))

    with open(artifact, "w", encoding="utf-8") as f:
        f.write("ok\n")
    r = _run([PY, RECEIPT, "confirm", "--id", eid], env=env)
    ok &= check("有产物 confirm PASS", r.returncode == 0, "rc=%d" % r.returncode)

    # ---- G. receipt 台账层（进程内，临时台账）：陈旧产物 / 同秒 id / 缺 port ----
    old_ledger = receipt.LEDGER_PATH
    receipt.LEDGER_PATH = os.path.join(td, "ledger2.jsonl")
    try:
        stale = os.path.join(td, "stale.md")
        with open(stale, "w", encoding="utf-8") as f:
            f.write("old\n")
        old_t = time.time() - 3600
        os.utime(stale, (old_t, old_t))
        receipt.main(["log", "--platform", "t-stale", "--task", "stale",
                      "--expect-file", stale])
        sid = [e["id"] for e in receipt._read_ledger() if e.get("platform") == "t-stale"][-1]
        rc = receipt.main(["confirm", "--id", sid])
        ok &= check("陈旧产物 confirm 必须 FAIL（不得 0.0s 假回执）", rc == 3, "rc=%d" % rc)

        receipt.main(["log", "--platform", "t-dup", "--task", "a", "--expect-file", "x"])
        receipt.main(["log", "--platform", "t-dup", "--task", "b", "--expect-file", "y"])
        ids = [e["id"] for e in receipt._read_ledger() if e.get("platform") == "t-dup"]
        ok &= check("同秒两次 log 的 id 唯一", len(ids) == 2 and len(set(ids)) == 2, str(ids))

        receipt.main(["log", "--platform", "t-noport", "--task", "no-port", "--marker", "M"])
        nid = [e["id"] for e in receipt._read_ledger() if e.get("platform") == "t-noport"][-1]
        crashed = ""
        try:
            rc = receipt.main(["wait", "--id", nid, "--timeout", "1", "--deadline", "2"])
        except Exception as e:  # noqa: BLE001
            crashed = "%s: %s" % (type(e).__name__, e)
        ok &= check("台账 port=null 的 marker 判定不崩", not crashed and rc != 0,
                    crashed or "rc=%d" % rc)
    finally:
        receipt.LEDGER_PATH = old_ledger

    # ---- H. deadline 是总时长（给了 deadline 就等它，不能先被 timeout 截断） ----
    r = receipt.wait_file(os.path.join(td, "never.md"), timeout=1,
                          deadline_at=time.time() + 2.5)
    ok &= check("deadline 生效（不再被 timeout 提前截断）",
                r.get("deadline") is True and r.get("secs", 0) >= 2.0, str(r))

    # ---- I. 并发 append 不丢条目（旧实现读-改-写会互相覆盖） ----
    conc = os.path.join(td, "conc.jsonl")
    child = (
        "import sys, os, time;"
        "sys.path.insert(0, %s);" % json.dumps(os.path.join(ROOT, "tools", "dispatch")) +
        "import receipt;"
        "receipt.LEDGER_PATH = os.environ['RECEIPT_LEDGER'];"
        "tag = sys.argv[1];"
        "[(receipt.ledger_append({'id': 'C-' + tag + '-' + str(i),"
        " 'ts': '2026-01-01 00:00:00', 'status': 'SENT'}), time.sleep(0.002)) for i in range(30)]"
    )
    cenv = dict(CHILD_ENV, RECEIPT_LEDGER=conc)
    p1 = subprocess.Popen([PY, "-c", child, "a"], env=cenv,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p2 = subprocess.Popen([PY, "-c", child, "b"], env=cenv,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p1.wait()
    p2.wait()
    with open(conc, "r", encoding="utf-8") as f:
        n = len([x for x in f.read().splitlines() if x.strip()])
    ok &= check("并发 append 不丢条目", n == 60, "lines=%d/60" % n)

    print("DISPATCH_CAPABILITY", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
