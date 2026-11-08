#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""平台能力表：加载 / schema 校验 / 任务卡 / 工作模式与模型预检（配置驱动）。

依据：
  * `instances/方案-统一记忆承载与实施层多平台化-v1-exec.md` D1 / C1 / C2 / C7

设计约束：
  * **fail-loud**：缺字段、枚举非法、端口重复、unprobed 被路由、模式不符，
    一律抛异常，不做默认值兜底。
  * 能力表只做查询；路由决策归 DSH 决策器（投递≠编排）。
  * 活动心跳等"判据函数只实现一份"的约束在 C3 落地，本文件只放模式预检。
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
from pathlib import Path

PLATFORM_IDS = ("qwen", "doubao", "dumate", "trae", "workbuddy")
WORK_MODES = ("chat", "local_computer", "cloud_computer")
CONFIRM_MODES = ("human", "auto", "advise")
STATUSES = ("active", "unprobed", "disabled")
READ_LOCAL = ("yes", "no", "unverified")
POLICIES = (None, "local_only")

REQUIRED_FIELDS = (
    "name",
    "port",
    "specialties",
    "specialties_verified",
    "read_local_files",
    "confirm_mode",
    "status",
    "work_mode",
    "work_mode_options",
    "work_mode_policy",
    "model",
    "model_default",
    "model_policy",
    "model_switch",
)
SELECTOR_FIELDS = ("trigger", "menu_open", "menu_item", "mode_text_map")


class PlatformTableError(Exception):
    """能力表非法 / 不该路由却路由（code 供判据断言）。"""

    def __init__(self, code: str, message: str):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


class ModeError(Exception):
    """工作模式无法确认或切换（必须转 blocked，不许降级发送）。"""

    def __init__(self, code: str, message: str, current: str = "", attempts: int = 0):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.current = current
        self.attempts = attempts


class ModelError(Exception):
    """模型无法确认或切换（配置要求目标模型时必须切换后才能发送）。"""

    def __init__(self, code: str, message: str, current: str = "", attempts: int = 0):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.current = current
        self.attempts = attempts


# ---------------------------------------------------------------------------
# 加载 / 校验
# ---------------------------------------------------------------------------


def default_path() -> Path:
    return Path(__file__).resolve().with_name("platforms.json")


def load(path=None) -> dict:
    p = Path(path) if path else default_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise PlatformTableError("TABLE_MISSING", "能力表不存在：%s" % p)
    except OSError as e:
        raise PlatformTableError("TABLE_IO", "能力表读取失败：%s（%s）" % (p, e))
    except json.JSONDecodeError as e:
        raise PlatformTableError("TABLE_JSON", "能力表不是合法 JSON：%s（%s）" % (p, e))
    validate(data)
    return data


def validate(data: dict) -> None:
    if not isinstance(data, dict):
        raise PlatformTableError("TABLE_SHAPE", "顶层不是对象")
    plats = data.get("platforms")
    if not isinstance(plats, dict) or not plats:
        raise PlatformTableError("TABLE_SHAPE", "platforms 不是非空对象")

    ports: dict[int, str] = {}
    for pid, p in plats.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", pid):
            raise PlatformTableError("INVALID_ID", "平台 id 非法：%r" % (pid,))
        if not isinstance(p, dict):
            raise PlatformTableError("PLATFORM_SHAPE", "%s 不是对象" % pid)

        for key in REQUIRED_FIELDS:
            if key not in p:
                raise PlatformTableError("MISSING_FIELD", "%s 缺字段 %s" % (pid, key))

        if not isinstance(p["name"], str) or not p["name"].strip():
            raise PlatformTableError("INVALID_NAME", "%s.name 非法" % pid)
        port = p["port"]
        if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
            raise PlatformTableError("INVALID_PORT", "%s.port=%r" % (pid, port))
        if port in ports:
            raise PlatformTableError(
                "PORT_DUP", "端口 %d 同时登记在 %s 与 %s" % (port, ports[port], pid)
            )
        ports[port] = pid

        specs = p["specialties"]
        if not isinstance(specs, list) or not specs or not all(isinstance(x, str) and x for x in specs):
            raise PlatformTableError("INVALID_SPECIALTIES", "%s.specialties 非法" % pid)
        if not isinstance(p["specialties_verified"], bool):
            raise PlatformTableError("INVALID_VERIFIED", "%s.specialties_verified 不是 bool" % pid)
        if p["read_local_files"] not in READ_LOCAL:
            raise PlatformTableError("INVALID_READ_LOCAL", "%s.read_local_files=%r" % (pid, p["read_local_files"]))
        if p["confirm_mode"] not in CONFIRM_MODES:
            raise PlatformTableError("INVALID_CONFIRM", "%s.confirm_mode=%r" % (pid, p["confirm_mode"]))
        if p["status"] not in STATUSES:
            raise PlatformTableError("INVALID_STATUS", "%s.status=%r" % (pid, p["status"]))
        if "session_switch" in p and p["session_switch"] not in ("yes", "no", "unknown"):
            raise PlatformTableError("INVALID_SESSION_SWITCH", "%s.session_switch=%r" % (pid, p["session_switch"]))

        options = p["work_mode_options"]
        if not isinstance(options, list) or any(x not in WORK_MODES for x in options):
            raise PlatformTableError("INVALID_MODE_OPTIONS", "%s.work_mode_options=%r" % (pid, options))
        policy = p["work_mode_policy"]
        if policy not in POLICIES:
            raise PlatformTableError("INVALID_MODE_POLICY", "%s.work_mode_policy=%r" % (pid, policy))

        if policy is None:
            if p["work_mode"] is not None:
                raise PlatformTableError(
                    "MODE_NOT_DECLARED", "%s 无 work_mode_policy 却声明 work_mode=%r" % (pid, p["work_mode"])
                )
        else:  # local_only
            if p["work_mode"] != "local_computer":
                raise PlatformTableError(
                    "MODE_NOT_ALLOWED", "%s 的 work_mode 必须是 local_computer（实为 %r）" % (pid, p["work_mode"])
                )
            if "local_computer" not in options:
                raise PlatformTableError("MODE_OPTIONS", "%s 的 options 未含 local_computer" % pid)
            if not p.get("mode_switch"):
                raise PlatformTableError("MODE_SWITCH", "%s 声明 local_only 但 mode_switch 不为 true" % pid)

        if p.get("mode_switch"):
            sel = p.get("work_mode_selectors")
            if not isinstance(sel, dict):
                raise PlatformTableError("MODE_SELECTORS", "%s 缺 work_mode_selectors" % pid)
            for key in SELECTOR_FIELDS:
                if key not in sel or not sel[key]:
                    raise PlatformTableError("MODE_SELECTORS", "%s 选择器缺 %s" % (pid, key))
            text_map = sel.get("mode_text_map")
            if not isinstance(text_map, dict) or set(text_map) != set(options):
                raise PlatformTableError(
                    "MODE_TEXT_MAP", "%s 的 mode_text_map 键与 options 不一致" % pid
                )
            if not all(isinstance(v, str) and v for v in text_map.values()):
                raise PlatformTableError("MODE_TEXT_MAP", "%s 的 mode_text_map 有非字符串值" % pid)
            if not mode_text_ok(text_map.get("local_computer", "")):
                raise PlatformTableError(
                    "MODE_TEXT_MAP", "%s 的 local_computer 文案 %r 不像本地模式" % (pid, text_map.get("local_computer"))
                )

        # ---- 模型预检（配置驱动） ----
        if not isinstance(p["model_switch"], bool):
            raise PlatformTableError("INVALID_MODEL_SWITCH", "%s.model_switch 不是 bool" % pid)
        if p["model"] is None:
            if p["model_default"] is not None or p["model_policy"] is not None:
                raise PlatformTableError(
                    "MODEL_NOT_DECLARED", "%s 无 model 却声明了 model_default/model_policy" % pid
                )
            if p["model_switch"]:
                raise PlatformTableError("MODEL_SWITCH", "%s 无 model 却 model_switch=true" % pid)
        else:
            if not isinstance(p["model"], str) or not p["model"].strip():
                raise PlatformTableError("INVALID_MODEL", "%s.model 非法" % pid)
            if not isinstance(p["model_default"], str) or not p["model_default"].strip():
                raise PlatformTableError("INVALID_MODEL_DEFAULT", "%s.model_default 非法" % pid)
            if p["model_policy"] != "required":
                raise PlatformTableError("INVALID_MODEL_POLICY", "%s.model_policy=%r" % (pid, p["model_policy"]))
            if p["model"] == p["model_default"]:
                raise PlatformTableError(
                    "MODEL_NOT_REQUIRED", "%s.model 与默认值相同（%r），无法形成预检" % (pid, p["model"])
                )
            if not p["model_switch"]:
                raise PlatformTableError("MODEL_SWITCH", "%s 声明 model 但 model_switch 不为 true" % pid)
            msel = p.get("model_selectors")
            if not isinstance(msel, dict):
                raise PlatformTableError("MODEL_SELECTORS", "%s 缺 model_selectors" % pid)
            for key in ("trigger", "value", "item"):
                if not msel.get(key):
                    raise PlatformTableError("MODEL_SELECTORS", "%s 模型选择器缺 %s" % (pid, key))


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def platforms(table=None) -> dict:
    return (table or load())["platforms"]


def get(pid: str, table=None) -> dict:
    plats = platforms(table)
    if pid not in plats:
        raise PlatformTableError("UNKNOWN_PLATFORM", "没有平台 %r" % pid)
    return plats[pid]


def routable(p: dict) -> bool:
    return p.get("status") == "active"


def require_routable(pid: str, table=None) -> dict:
    p = get(pid, table)
    if not routable(p):
        raise PlatformTableError(
            "NOT_ROUTABLE", "%s.status=%r（unprobed/disabled 不可路由）" % (pid, p.get("status"))
        )
    return p


# ---------------------------------------------------------------------------
# 任务卡（C7）
# ---------------------------------------------------------------------------


def make_task_card(
    platform: dict,
    task_id: str,
    task_type: str,
    *,
    confirm_mode: str | None = None,
    deadline: int | None = None,
    marker_suffix: str | None = None,
    background_files=None,
    acceptance=None,
) -> dict:
    if not isinstance(task_id, str) or not task_id.strip():
        raise PlatformTableError("INVALID_TASK_ID", "task_id 非法：%r" % (task_id,))
    if not isinstance(task_type, str) or not task_type.strip():
        raise PlatformTableError("INVALID_TASK_TYPE", "task_type 非法：%r" % (task_type,))
    if not routable(platform):
        raise PlatformTableError("NOT_ROUTABLE", "平台 status=%r，不能生成任务卡" % platform.get("status"))

    cm = confirm_mode or platform["confirm_mode"]
    if cm not in CONFIRM_MODES:
        raise PlatformTableError("INVALID_CONFIRM", "confirm_mode=%r" % (cm,))
    if deadline is not None and (not isinstance(deadline, int) or isinstance(deadline, bool) or deadline <= 0):
        raise PlatformTableError("INVALID_DEADLINE", "deadline=%r（应为正整数秒或 None）" % (deadline,))
    suffix = marker_suffix if marker_suffix is not None else secrets.token_hex(4)
    if not re.fullmatch(r"[0-9a-f]{8}", suffix):
        raise PlatformTableError("INVALID_MARKER", "marker_suffix=%r（应为 8 位小写 hex）" % (suffix,))

    bg = list(background_files or [])
    ac = list(acceptance or [])
    if not all(isinstance(x, str) and x for x in bg + ac):
        raise PlatformTableError("INVALID_TASK_CARD", "background_files/acceptance 必须是非空字符串列表")

    return {
        "task_id": task_id,
        "type": task_type,
        "confirm_mode": cm,
        "deadline": deadline,
        "marker_suffix": suffix,
        "background_files": bg,
        "acceptance": ac,
        "work_mode": platform.get("work_mode"),
        "mode_switch_state": "pending" if platform.get("mode_switch") else "not_applicable",
        "model": platform.get("model"),
        "model_switch_state": "pending" if platform.get("model_switch") else "not_applicable",
    }


# ---------------------------------------------------------------------------
# 工作模式预检（配置驱动）
# ---------------------------------------------------------------------------

MODE_TEXT_LOCAL = "本地电脑"
MODE_TEXT_CLOUD = "云电脑"


def mode_text_ok(text: str) -> bool:
    """归一化后判「本地电脑」：去空白，必须含本地电脑且不含云电脑。"""
    norm = re.sub(r"\s+", "", text or "")
    return MODE_TEXT_LOCAL in norm and MODE_TEXT_CLOUD not in norm


_LOCKS: dict[int, asyncio.Lock] = {}


def page_lock(port: int) -> asyncio.Lock:
    """同页真实鼠标是全局动作：dry-run/status/派发/模式/模型必须串行。"""
    lock = _LOCKS.get(port)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[port] = lock
    return lock


# 兼容旧名（C2 早期写法）
mode_lock = page_lock


async def _ev_json(page, expr):
    raw = await page.ev(expr)
    if not raw:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


async def _read_mode(page, trigger: str) -> str:
    expr = (
        "(() => { const b = document.querySelector(%s);"
        " return b ? (b.innerText || '') : null; })()" % json.dumps(trigger)
    )
    text = await page.ev(expr)
    if text is None:
        raise ModeError("MODE_TRIGGER_MISSING", "找不到模式按钮 %s" % trigger, "", 0)
    return text.strip()


async def _box(page, selector: str, text_ok=None):
    expr = """
    (() => {
      const sel = %s;
      const ok = %s;
      const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden'; };
      const all = [...document.querySelectorAll(sel)].filter(vis);
      const hit = ok ? all.find(e => ok((e.innerText || '').trim())) : all[0];
      if (!hit) return null;
      const r = hit.getBoundingClientRect();
      return JSON.stringify({x: r.x + r.width / 2, y: r.y + r.height / 2,
                             text: (hit.innerText || '').trim()});
    })()
    """ % (
        json.dumps(selector),
        "(t) => true" if text_ok is None else text_ok,
    )
    return await _ev_json(page, expr)


async def _click_box(page, box) -> None:
    for typ in ("mousePressed", "mouseReleased"):
        await page.send(
            "Input.dispatchMouseEvent",
            type=typ,
            x=box["x"],
            y=box["y"],
            button="left",
            clickCount=1,
        )


async def _wait_menu(page, menu_sel: str, timeout_s: float = 3.0) -> None:
    expr = (
        "(() => { const e = document.querySelector(%s); if (!e) return false;"
        " const r = e.getBoundingClientRect(); const s = getComputedStyle(e);"
        " return r.width > 0 && r.height > 0 && s.display !== 'none'; })()"
        % json.dumps(menu_sel)
    )
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if await page.ev(expr):
            return
        await asyncio.sleep(0.15)
    raise ModeError("MODE_MENU_TIMEOUT", "模式菜单 %.1fs 内未出现" % timeout_s)


async def ensure_work_mode(page, platform: dict) -> dict:
    """确保平台处于配置允许的工作模式。

    返回 {"work_mode": ..., "mode_switch_state": "already|switched|not_applicable"}
    失败抛 ModeError；调用方必须转 blocked，**不许降级发送**。
    """
    if not platform.get("mode_switch"):
        return {"work_mode": platform.get("work_mode"), "mode_switch_state": "not_applicable"}

    policy = platform.get("work_mode_policy")
    if policy != "local_only":
        raise ModeError("MODE_POLICY", "mode_switch=true 但 policy=%r" % (policy,), "", 0)

    sel = platform.get("work_mode_selectors") or {}
    trigger, menu_sel, item_sel = sel.get("trigger"), sel.get("menu_open"), sel.get("menu_item")
    if not (trigger and menu_sel and item_sel):
        raise ModeError("MODE_SELECTORS_MISSING", "缺模式选择器", "", 0)

    text_map = sel.get("mode_text_map") or {}
    local_text = text_map.get("local_computer", MODE_TEXT_LOCAL)
    cloud_text = text_map.get("cloud_computer", MODE_TEXT_CLOUD)

    port = platform.get("port", 0)
    async with page_lock(port):
        current = await _read_mode(page, trigger)
        if mode_text_ok(current):
            return {"work_mode": "local_computer", "mode_switch_state": "already"}

        attempts = 0
        for attempts in (1, 2):
            try:
                box = await _box(page, trigger)
                if not box:
                    raise ModeError("MODE_TRIGGER_MISSING", "模式按钮不可见", current, attempts)
                await _click_box(page, box)
                await _wait_menu(page, menu_sel)

                ok = "(t) => t.includes(%s) && !t.includes(%s)" % (
                    json.dumps(local_text),
                    json.dumps(cloud_text),
                )
                item = await _box(page, item_sel, ok)
                if not item:
                    raise ModeError("MODE_ITEM_MISSING", "菜单里没有「%s」" % local_text, current, attempts)
                await _click_box(page, item)
                current = await _read_mode(page, trigger)
                if mode_text_ok(current):
                    return {"work_mode": "local_computer", "mode_switch_state": "switched"}
            except ModeError:
                continue

        raise ModeError(
            "MODE_UNAVAILABLE",
            "无法确认/切换到「%s」（当前 %r）" % (local_text, current),
            current,
            attempts,
        )


# ---------------------------------------------------------------------------
# 模型预检（配置驱动）
# ---------------------------------------------------------------------------


def model_matches(current: str, target: str) -> bool:
    return (current or "").strip() == (target or "").strip()


async def _read_text(page, selector: str, code: str) -> str:
    expr = (
        "(() => { const e = document.querySelector(%s);"
        " return e ? (e.innerText || '') : null; })()" % json.dumps(selector)
    )
    text = await page.ev(expr)
    if text is None:
        raise ModelError(code, "找不到元素 %s" % selector, "", 0)
    return text.strip()


async def _wait_item(page, selector: str, target: str, timeout_s: float = 3.0):
    predicate = "(t) => t === %s" % json.dumps(target)
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        item = await _box(page, selector, predicate)
        if item:
            return item
        await asyncio.sleep(0.15)
    return None


async def ensure_model(page, platform: dict) -> dict:
    """确保平台处于配置要求的模型。

    返回 {"model": ..., "model_switch_state": "already|switched|not_applicable"}
    失败抛 ModelError；调用方必须转 blocked，**不许按默认值发送**。
    """
    if not platform.get("model_switch"):
        return {"model": platform.get("model"), "model_switch_state": "not_applicable"}

    if platform.get("model_policy") != "required":
        raise ModelError("MODEL_POLICY", "model_switch=true 但 policy=%r" % (platform.get("model_policy"),), "", 0)

    sel = platform.get("model_selectors") or {}
    trigger, value_sel, item_sel = sel.get("trigger"), sel.get("value"), sel.get("item")
    if not (trigger and value_sel and item_sel):
        raise ModelError("MODEL_SELECTORS_MISSING", "缺模型选择器", "", 0)

    target = platform.get("model") or ""
    if not target:
        raise ModelError("MODEL_TARGET_MISSING", "能力表没有目标模型", "", 0)

    port = platform.get("port", 0)
    async with page_lock(port):
        current = await _read_text(page, value_sel, "MODEL_TRIGGER_MISSING")
        if model_matches(current, target):
            return {"model": target, "model_switch_state": "already"}

        attempts = 0
        for attempts in (1, 2):
            try:
                # 菜单可能已开着：先直接找目标项
                item = await _wait_item(page, item_sel, target, 0.3)
                if not item:
                    trig = await _box(page, trigger)
                    if not trig:
                        raise ModelError("MODEL_TRIGGER_MISSING", "模型按钮不可见", current, attempts)
                    await _click_box(page, trig)
                    item = await _wait_item(page, item_sel, target, 3.0)
                if not item:
                    raise ModelError("MODEL_ITEM_MISSING", "模型列表里没有 %r" % target, current, attempts)
                await _click_box(page, item)
                current = await _read_text(page, value_sel, "MODEL_TRIGGER_MISSING")
                if model_matches(current, target):
                    return {"model": target, "model_switch_state": "switched"}
            except ModelError:
                continue

        raise ModelError(
            "MODEL_UNAVAILABLE",
            "无法确认/切换到模型「%s」（当前 %r）" % (target, current),
            current,
            attempts,
        )
