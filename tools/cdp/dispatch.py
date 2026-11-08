#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""dispatch.py — 往本机 Chromium 系 AI 应用投一段文字并读回（走 CDP）。

适用范围：通过 `--remote-debugging-port` 开出 CDP 的桌面应用（Electron/Chromium 系）。
平台/端口/页面选择器由 `tools/dispatch/platforms.json` 与代码里的选择器表配置。

默认行为是「只填入、不发送」（dry-run）：填入 → 读回确认 → 清空输入框，零副作用。
要真发送必须显式加 `--send`。

三条安全约束：
  1. **输入框非空就拒绝写入** —— 你很可能正在打字，覆盖等于毁你的内容。除非 `--force`。
  2. **不读历史对话内容** —— 只读输入框里刚写的那句话和发送后新增的响应。
  3. **不做删除/清空之外的操作** —— 不点侧栏、不切会话、不动设置。

用法：
  python dispatch.py --port <PORT> --status
  python dispatch.py --port <PORT> --text "你好"                 # dry-run
  python dispatch.py --port <PORT> --text "你好" --send           # 真发送并等回复
  python dispatch.py --port <PORT> --text "你好" --send --timeout 90
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import websockets
except ImportError:
    sys.stderr.write("MISSING: 需要 websockets（pip install websockets）\n")
    sys.exit(2)

# 平台能力表：端口登记 + 工作模式/模型预检（配置驱动）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dispatch"))
import platforms as PT  # noqa: E402

# 本机请求必须绕过系统代理：环境里的 HTTP(S)_PROXY 会劫持 127.0.0.1（实测报 502）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 页面优先级（按 url 子串）。顺序即优先级，先命中的胜出。
# 选错窗口会把字打进隐形/后台页 —— 报成功 ≠ 打对地方。
PAGE_PREF = {
    9555: ("windowId=main", "index.html", "#win"),
    9222: ("",),
    9444: ("solo-lite", "workbench", "index.html"),  # 目标窗口优先（见 _guard_trae）
    9333: ("doubao-launcher", "doubao-chat", ""),
    9666: ("",),
}
DEFAULT_PREF = ("",)

# 发送按钮选择器：按目标应用实际 DOM 结构填写。
# 用属性包含匹配优先，避免类名截断/版本差异导致选不到按钮。
SEND_SEL = {
    9555: 'button[aria-label="发送消息"]',
    9222: 'button[aria-label="发送"]',
    9333: '[class*="send-btn"]',
    9666: 'button[title="发送"]',
    9444: '.chat-input-v2-send-button',
}
SEL_EDITOR = '[contenteditable="true"]'

# 思考指示器（D3）：生成中存在的信号；完成 = 指示器消失且文本稳定
THINK_SEL = {
    9555: 'button[aria-label="停止"]',
    9333: '.animate-spin,[class*="dot-flashing"],[class*="loading-container"]',
    9666: '[class*="thinkingPlaceholder"]',
    9444: '',  # 无指示器时退化为纯文本稳定
}

# 写入前检查输入框是否为空的容差（纯空白视为空）
def _blank(s: str) -> bool:
    return not (s or "").strip()


class Page:
    """一个 CDP page 的最小封装：send/eval。"""

    def __init__(self, ws, url=""):
        self.ws = ws
        self.url = url
        self._id = 0

    async def send(self, method, **params):
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            m = json.loads(await asyncio.wait_for(self.ws.recv(), 30))
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError("%s → %s" % (method, m["error"]))
                return m.get("result", {})

    async def ev(self, expr):
        r = await self.send("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            raise RuntimeError("JS 异常：%s" % json.dumps(r["exceptionDetails"], ensure_ascii=False)[:200])
        return (r.get("result") or {}).get("value")

    async def ev_json(self, expr):
        v = await self.ev(expr)
        return json.loads(v) if isinstance(v, str) else v


def _guard_trae(port, target):
    """页面守卫：自动选中聊天栏而不是 Agent 窗口时必须 fail-loud。
    聊天栏会回"完成"但不写工作区 —— 最贵的假成功路径。"""
    url = target.get("url") or ""
    if port == 9444 and "workbench" in url and "solo-lite" not in url:
        raise RuntimeError(
            "自动选中的是聊天栏，不是 Agent 窗口；请用 --match 指定正确窗口，或先切换窗口")
    return target


def _safe_url(url: str) -> str:
    """只回 host + 路径末段；绝不打印 query/fragment（可能含 token/cookie）。"""
    try:
        parts = urllib.parse.urlsplit(url or "")
        return (parts.netloc or "") + (parts.path[-60:] if parts.path else "")
    except Exception:
        return "(page)"


def pick_page(port, match=None):
    """选 page。返回 (target_dict, 序号, 总数, 依据)。"""
    with _OPENER.open("http://127.0.0.1:%d/json/list" % port, timeout=4) as r:
        targets = json.load(r)
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        raise RuntimeError("没有可用的 page target（应用是不是没带调试端口启动？）")

    if match:
        for i, t in enumerate(pages, 1):
            if match in (t.get("url") or ""):
                return t, i, len(pages), "--match %s" % match
        raise RuntimeError("没有 url 含 %r 的 page；现有尾部：%s"
                           % (match, [(t.get("url") or "")[-40:] for t in pages]))

    for pref in PAGE_PREF.get(port, DEFAULT_PREF):
        if not pref:
            continue
        for i, t in enumerate(pages, 1):
            if pref in (t.get("url") or ""):
                return _guard_trae(port, t), i, len(pages), "优先匹配 %r" % pref
    return _guard_trae(port, pages[0]), 1, len(pages), "无匹配，退回第一个"


async def locate(page, send_sel):
    """定位输入框与发送按钮。返回 dict（找不到的项为 None）。"""
    return await page.ev_json("""
(() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.top >= 0 && r.bottom <= innerHeight + 1; };
  const eds = Array.from(document.querySelectorAll('%s')).filter(vis);
  eds.sort((a,b) => { const ra=a.getBoundingClientRect(), rb=b.getBoundingClientRect();
                      return (rb.width*rb.height) - (ra.width*ra.height); });
  const ed = eds[0] || null;
  const send = document.querySelector('%s');
  const r = ed ? ed.getBoundingClientRect() : null;
  return JSON.stringify({
    editor: ed ? {found:true, cls:(ed.className||'').toString().slice(0,60),
                  w:Math.round(r.width), h:Math.round(r.height),
                  textLen:(ed.innerText||'').length} : {found:false},
    send: send ? {found:true, aria:(send.getAttribute('aria-label')
                   || send.getAttribute('title') || send.className || '').toString().slice(0,30),
                  disabled: !!send.disabled} : {found:false},
    editorCount: eds.length
  });
})()
""" % (SEL_EDITOR, send_sel))


async def type_text(page, text):
    """把文本写进输入框。用 CDP 的 Input.insertText —— 它会触发真实输入事件，
    比直接改 innerText 可靠（后者对 React 受控组件常常不生效）。"""
    # ★ WorkBuddy 2026-10-29 实测：只 focus() 时 insertText 读回为空/换行，
    #   必须先发真实鼠标 click 到编辑器中心，框架才接受输入。
    try:
        rect = json.loads(await page.ev_json("""
(() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.top >= 0 && r.bottom <= innerHeight + 1; };
  const eds = Array.from(document.querySelectorAll('%s')).filter(vis);
  if (!eds.length) return JSON.stringify(null);
  const r = eds[0].getBoundingClientRect();
  return JSON.stringify({x:r.x+r.width/2, y:r.y+r.height/2});
})()
""" % SEL_EDITOR))
        if rect:
            await page.send("Input.dispatchMouseEvent", type="mousePressed", x=rect["x"], y=rect["y"], button="left", clickCount=1)
            await page.send("Input.dispatchMouseEvent", type="mouseReleased", x=rect["x"], y=rect["y"], button="left", clickCount=1)
            await asyncio.sleep(0.15)
    except Exception:
        pass
    # 先聚焦
    ok = await page.ev("""
(() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.top >= 0 && r.bottom <= innerHeight + 1; };
  const eds = Array.from(document.querySelectorAll('%s')).filter(vis);
  if (!eds.length) return false;
  eds[0].focus();
  return document.activeElement === eds[0];
})()
""" % SEL_EDITOR)
    if not ok:
        raise RuntimeError("输入框聚焦失败")
    # ★ 必须先清空再写：`Input.insertText` 是**在光标处插入**，不是覆盖。
    #   2026-10-28 实测踩到：输入框有上次残留时，直接 insertText 导致
    #   发出去的是「请只回复两个字：收到请只回复两个字：收到」——内容翻倍。
    await clear_editor(page)
    await page.ev("""(() => { const vis = e => { const r = e.getBoundingClientRect();
        return r.width>0 && r.height>0; };
      const eds = Array.from(document.querySelectorAll('%s')).filter(vis);
      if (eds.length) eds[0].focus(); return true; })()
""" % SEL_EDITOR)
    await page.send("Input.insertText", text=text)
    await asyncio.sleep(0.3)
    return await page.ev("(([...document.querySelectorAll('%s')].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&r.top>=0&&r.bottom<=innerHeight+1;})[0])||{}).innerText || ''" % SEL_EDITOR)


async def clear_editor(page):
    """清空输入框：Ctrl+A + Backspace（3 次），再兜底清 DOM。

    ★ 实测：`document.execCommand('selectAll')` 对部分富文本编辑器只清掉一部分，
      必须用真实 Ctrl+A。
    """
    async def focus():
        await page.ev("""
(() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.top >= 0 && r.bottom <= innerHeight + 1; };
  const eds = Array.from(document.querySelectorAll('%s')).filter(vis);
  if (eds.length) eds[0].focus();
  return true;
})()
""" % SEL_EDITOR)

    for _ in range(3):
        await focus()
        for t in ("keyDown", "keyUp"):
            await page.send("Input.dispatchKeyEvent", type=t, key="a", code="KeyA",
                            windowsVirtualKeyCode=65, nativeVirtualKeyCode=65, modifiers=2)
        await asyncio.sleep(0.05)
        for t in ("keyDown", "keyUp"):
            await page.send("Input.dispatchKeyEvent", type=t, key="Backspace",
                            code="Backspace", windowsVirtualKeyCode=8, nativeVirtualKeyCode=8)
        await asyncio.sleep(0.15)
        text = await page.ev("(([...document.querySelectorAll('%s')].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&r.top>=0&&r.bottom<=innerHeight+1;})[0])||{}).innerText || ''" % SEL_EDITOR)
        if _blank(text):
            return text

    # 兜底：只剩空白/清不动时直接清 DOM（Lexical 会自行重渲染）
    return await page.ev("""
(() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.top >= 0 && r.bottom <= innerHeight + 1; };
  const eds = Array.from(document.querySelectorAll('%s')).filter(vis);
  if (!eds.length) return '';
  const ed = eds[0];
  ed.focus();
  ed.innerHTML = '<p><br></p>';
  ed.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'deleteContentBackward'}));
  return ed.innerText || '';
})()
""" % SEL_EDITOR)


async def click_send(page, send_sel):
    """点发送。send_sel 由平台注册表给出（SEND_SEL）。

    ★ 2026-10-28 实测教训：`element.click()`（程序化点击）**对千问无效** ——
      点了之后输入框内容还在、没有任何反应。截图确认按钮是可用的（黑底箭头、未禁用）。
      原因大概率是它监听真实指针事件（pointerdown/mousedown），不吃合成 click。
      ⇒ 改用 CDP 的 `Input.dispatchMouseEvent` 发**真实鼠标事件**（含 hover → press → release），
        失败再用 Enter 键兜底。这才是"真实用户点击"。
    """
    box = await page.ev_json("""
(() => { const b = document.querySelector('%s');
  if (!b) return JSON.stringify({found:false});
  const r = b.getBoundingClientRect();
  return JSON.stringify({found:true, disabled: !!b.disabled,
    x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)}); })()
""" % send_sel)
    if not box.get("found"):
        return json.dumps({"ok": False, "why": "找不到发送按钮 %s" % send_sel}, ensure_ascii=False)
    if box.get("disabled"):
        # ★ 竞态：insertText 后 React/框架把"可发送"状态置回有延迟（搭子实测），
        #   立刻查会撞上禁用态。等最多 4 秒再放弃。
        enabled = False
        for _ in range(8):
            await asyncio.sleep(0.5)
            box = await page.ev_json("""
(() => { const b = document.querySelector('%s');
  return b ? JSON.stringify({found:true, disabled: !!b.disabled,
    x: Math.round((b.getBoundingClientRect().left)+b.getBoundingClientRect().width/2),
    y: Math.round((b.getBoundingClientRect().top)+b.getBoundingClientRect().height/2)})
    : JSON.stringify({found:false}); })()
""" % send_sel)
            if not box.get("found"):
                return json.dumps({"ok": False, "why": "等待期间发送按钮消失了"}, ensure_ascii=False)
            if not box.get("disabled"):
                enabled = True
                break
        if not enabled:
            return json.dumps({"ok": False, "why": "发送按钮持续禁用 4s（输入可能没被框架接受）"},
                              ensure_ascii=False)

    x, y = box["x"], box["y"]
    # 1) 先移入（有些组件靠 hover 才绑定按下逻辑）
    await page.send("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
    await asyncio.sleep(0.15)
    # 2) 真实按下 + 抬起
    await page.send("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y,
                    button="left", buttons=1, clickCount=1)
    await asyncio.sleep(0.08)
    await page.send("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y,
                    button="left", buttons=0, clickCount=1)

    # 3) 判据：发送成功的最硬信号是「输入框被清空」
    for _ in range(10):
        await asyncio.sleep(0.4)
        left = await page.ev("(([...document.querySelectorAll('%s')].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&r.top>=0&&r.bottom<=innerHeight+1;})[0])||{}).innerText || ''" % SEL_EDITOR)
        if _blank(left):
            return json.dumps({"ok": True, "how": "鼠标点击（输入框已清空）"}, ensure_ascii=False)

    # 4) 兜底：回车发送
    await page.ev("""(() => { const vis = e => { const r = e.getBoundingClientRect();
        return r.width>0 && r.height>0; };
      const eds = Array.from(document.querySelectorAll('%s')).filter(vis);
      if (eds.length) eds[0].focus(); return true; })()
""" % SEL_EDITOR)
    for t in ("keyDown", "keyUp"):
        await page.send("Input.dispatchKeyEvent", type=t, key="Enter", code="Enter",
                        windowsVirtualKeyCode=13, nativeVirtualKeyCode=13,
                        )
    for _ in range(10):
        await asyncio.sleep(0.4)
        left = await page.ev("(([...document.querySelectorAll('%s')].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&r.top>=0&&r.bottom<=innerHeight+1;})[0])||{}).innerText || ''" % SEL_EDITOR)
        if _blank(left):
            return json.dumps({"ok": True, "how": "Enter 键（输入框已清空）"}, ensure_ascii=False)
    return json.dumps({"ok": False, "why": "点击与回车都没让输入框清空"}, ensure_ascii=False)


def send_disabled_js(send_sel):
    return """(() => { const b = document.querySelector('%s');
  return b ? !!b.disabled : null; })()""" % send_sel


def activity_js(port):
    sel = THINK_SEL.get(port, "")
    think = "false" if not sel else "!!document.querySelector(%s)" % json.dumps(sel)
    return """(() => {
      const t = (document.body.innerText || '').length;
      const st = (document.scrollingElement || document.body).scrollTop || 0;
      return JSON.stringify({t, thinking: %s, st});
    })()""" % think


async def wait_reply(page, before_len, timeout, send_sel=None, mode="stable", port=None, deadline=None):
    """D3：判完成 ≠ 判挂死。

    完成：思考指示器消失 ＋ 文本连续 2 轮稳定（且已过 2s 启动窗）。
    挂死：连续 timeout 秒没有任何活动迹象（文本/思考指示器/滚动，任一变化重置）。
    总时长：`deadline` 显式给值才封顶；默认不设限（长任务 4h+ 属常态）。
    返回 (status, delta, secs)，status ∈ DONE / HANG / DEADLINE。
    """
    t0 = time.time()
    hard = (t0 + deadline) if deadline else None
    last = None
    last_change = t0
    stable = 0

    while True:
        now = time.time()
        if hard is not None and now >= hard:
            cur = await page.ev("(document.body.innerText||'').length")
            return "DEADLINE", (cur - before_len) if isinstance(cur, int) else 0, round(now - t0, 1)

        sig = await page.ev_json(activity_js(port))
        cur = sig.get("t") if isinstance(sig.get("t"), int) else 0
        thinking = bool(sig.get("thinking"))
        key = (cur, thinking, sig.get("st"))

        if key != last:
            last = key
            last_change = now
            stable = 0
        else:
            stable += 1
        # 思考指示器在场视为"还在动"（spinner 动画本身是活动信号）
        if thinking:
            last_change = now
        if stable >= 2 and not thinking and cur > before_len and now - t0 >= 2.0:
            return "DONE", cur - before_len, round(now - t0, 1)
        if now - last_change >= timeout:
            return "HANG", cur - before_len, round(now - t0, 1)
        await asyncio.sleep(1.0)


def platform_for(port):
    """端口 → (平台 id, 能力表条目)。未登记 ⇒ fail-loud。"""
    try:
        table = PT.load()
    except PT.PlatformTableError as e:
        raise RuntimeError("能力表不可用：%s: %s" % (e.code, e.message))
    for pid, p in table["platforms"].items():
        if p.get("port") == port:
            return pid, p
    raise RuntimeError("端口 %d 未登记在能力表 platforms.json" % port)


async def main_async(a):
    t, idx, total, why = pick_page(a.port, a.match)
    send_sel = SEND_SEL.get(a.port, SEND_SEL[9555])
    pid, platform = platform_for(a.port)
    print("[page] %d/%d（%s）" % (idx, total, why))
    print("[url ] %s" % _safe_url(t.get("url") or ""))
    print("[平台] %s / %s / status=%s" % (pid, platform["name"], platform["status"]))

    async with websockets.connect(t["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as ws:
        page = Page(ws, t.get("url") or "")
        loc = await locate(page, send_sel)
        print("[元素] 输入框 %s ／ 发送按钮 %s ／ 可编辑元素共 %d 个"
              % ("找到 %dx%d" % (loc["editor"]["w"], loc["editor"]["h"]) if loc["editor"]["found"] else "**未找到**",
                 "找到（aria=%s）" % loc["send"]["aria"] if loc["send"]["found"] else "**未找到**",
                 loc["editorCount"]))

        if a.status:
            if loc["editor"]["found"]:
                print("[状态] 输入框现有文本长度 = %d%s"
                      % (loc["editor"]["textLen"], "（非空，可能是你正在输入）" if loc["editor"]["textLen"] else ""))
            if not loc["send"]["found"]:
                print("[提示] 发送按钮未找到——部分应用在空输入时不渲染发送按钮，填入文字后才出现（非故障）")
            return 0 if (loc["editor"]["found"] and loc["send"]["found"]) else 1

        if not loc["editor"]["found"]:
            raise RuntimeError("找不到输入框，无法投递")
        existing = await page.ev("(([...document.querySelectorAll('%s')].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&r.top>=0&&r.bottom<=innerHeight+1;})[0])||{}).innerText || ''" % SEL_EDITOR)
        if not _blank(existing) and existing.strip() != (a.text or "").strip():
            if not a.force:
                raise RuntimeError("输入框里已有别的内容（长度 %d），拒绝覆盖 —— 你很可能正在输入。"
                                   "确认可覆盖请加 --force" % len(existing))
        elif not _blank(existing):
            print("[提示] 输入框里有上次尝试的残留（内容相同），直接复用/覆盖")

        # ---- 预检（配置驱动）：工作模式 / 模型；失败不填字、不发送 ----
        if not PT.routable(platform):
            if not a.probe:
                raise RuntimeError(
                    "平台 %s.status=%r 不可路由（unprobed 须先勘查；--probe 只允许 status/dry-run）"
                    % (pid, platform["status"])
                )
            if a.send:
                raise RuntimeError("--probe 不允许 --send")
        try:
            work = await PT.ensure_work_mode(page, platform)
            # Agent 窗口把模型放在会话预设里，没有模型选择器；
            # 该窗口由宿主/用户预设模型，预检按 agent-preset 放行。
            if pid == "trae" and "solo-lite" in (page.url or ""):
                # Agent 窗口的模型由会话预设决定；视为已配置。
                model = {"model": str(platform.get("model") or "agent-preset"), "model_switch_state": "agent-preset"}
            else:
                model = await PT.ensure_model(page, platform)
        except (PT.ModeError, PT.ModelError) as e:
            print("[预检] FAIL %s: %s" % (e.code, e.message))
            print("RESULT: FAIL（预检未过：未填字、未发送）")
            return 2
        print("[预检] work_mode=%s/%s  model=%s/%s"
              % (work.get("work_mode"), work.get("mode_switch_state"),
                 model.get("model"), model.get("model_switch_state")))

        # ---- 填入 ----
        got = await type_text(page, a.text)
        print("[填入] 读回 = %r" % (got or "")[:120])
        filled_ok = (got or "").strip() == a.text.strip()
        print("[校验] 写入内容与预期%s" % ("一致" if filled_ok else "**不一致**"))

        if not a.send:
            cleared = await clear_editor(page)
            print("[清空] 清空后 = %r" % (cleared or "")[:60])
            print("RESULT: %s（dry-run，未发送；输入框已还原为空）"
                  % ("PASS" if filled_ok and _blank(cleared) else "FAIL"))
            return 0 if (filled_ok and _blank(cleared)) else 1

        # ---- 真发送 ----
        r = json.loads(await click_send(page, send_sel))
        print("[发送] %s" % ("已点击" if r.get("ok") else "失败：%s" % r.get("why")))
        if not r.get("ok"):
            return 1
        # ★ 发送后输入框已清空才取基线：否则把输入框文字算进回复增量，delta 会变负数
        await asyncio.sleep(0.3)
        before = await page.ev("(document.body.innerText||'').length")
        status, delta, secs = await wait_reply(page, int(before), a.timeout,
                                               send_sel=send_sel, port=a.port,
                                               deadline=a.deadline)
        label = {"DONE": "完成", "HANG": "挂死（连续无活动）",
                 "DEADLINE": "DEADLINE（总时长封顶）"}.get(status, status)
        print("[回复] %s（%d 字新增，用时 %ss）" % (label, delta, secs))
        left = await page.ev("(([...document.querySelectorAll('%s')].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&r.top>=0&&r.bottom<=innerHeight+1;})[0])||{}).innerText || ''" % SEL_EDITOR)
        print("[输入框] 发送后 = %r" % (left or "")[:60])
        ok = status == "DONE"
        print("RESULT: %s%s" % ("PASS" if ok else "FAIL", "" if ok else "(%s)" % status))
        return 0 if ok else 1


def main(argv):
    ap = argparse.ArgumentParser(description="往本机 AI 平台投一段文字并读回（CDP）")
    ap.add_argument("--port", type=int, required=True,
                    help="调试端口（见 tools/dispatch/platforms.json）")
    ap.add_argument("--text", default=None, help="要投递的文字")
    ap.add_argument("--status", action="store_true", help="只报状态与元素定位，不做任何写入")
    ap.add_argument("--send", action="store_true", help="真的点发送（默认不发送）")
    ap.add_argument("--force", action="store_true", help="输入框非空时也覆盖（默认拒绝）")
    ap.add_argument("--match", default=None, help="只连 url 含该子串的 page")
    ap.add_argument("--timeout", type=int, default=300,
                    help="无活动超时秒数（默认 300；不是总时长）")
    ap.add_argument("--deadline", type=int, default=None,
                    help="总时长上限秒（默认无限；超期报 DEADLINE）")
    ap.add_argument("--probe", action="store_true",
                    help="允许对 unprobed 平台做 status/dry-run（禁止 --send）")
    a = ap.parse_args(argv)

    if not a.status and not a.text:
        ap.error("要么给 --status，要么给 --text")
    try:
        return asyncio.run(main_async(a))
    except Exception as e:
        print("RESULT: FAIL —— %s: %s" % (type(e).__name__, e))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
