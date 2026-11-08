/**
 * @local/dsh-dispatch — Host plugin (unified external-dispatch entry).
 *
 * One tool `dispatch` (doctor/platforms/send/wait/confirm/list) and one Service
 * `dispatch` (same actions) so context-manager can call it without shelling out:
 *
 *   const dispatch = ctx.get('dispatch')
 *   await dispatch.doctor()
 *   await dispatch.send({ platform: 'qwen', text, send: true,
 *                          expect_file: '...', wait: true })
 *
 * Hard rules encoded here (see tools/dispatch/README.md):
 *   - doctor first; CLOSED is "not started", not "cannot dispatch";
 *   - page match must target the intended window (see DEFAULT_MATCH / --match);
 *   - completion is artifact-based: receipt.confirm refuses a missing artifact;
 *   - no token/raw URL in returned page data (doctor already redacts).
 */
import { execFile } from 'node:child_process'
import fs from 'node:fs/promises'
import path from 'node:path'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)

export const name = 'dispatch-manager'
export const inject = ['tools']

const DEFAULTS = {
  repoRoot: process.cwd(),
  python: 'python',
  toolsDir: 'tools/dispatch',
  cdpDir: 'tools/cdp',
  allowSend: true,
  defaultDeadlineS: 900,
  defaultTimeoutS: 300,
  maxOutputChars: 12000,
}

const PLATFORMS = ['qwen', 'doubao', 'dumate', 'trae', 'workbuddy']
const DEFAULT_MATCH = {
  qwen: 'windowId=main',
  doubao: 'doubao-launcher',
  trae: 'solo-lite',
}
const ACTIONS = ['doctor', 'platforms', 'send', 'wait', 'confirm', 'list']
const STATUSES = ['SENT', 'REPLIED', 'TIMEOUT', 'FAILED', 'RETRY-SENT', 'ESCALATED', 'DEADLINE']

function resolveConfig(config) {
  const c = { ...DEFAULTS, ...(config && typeof config === 'object' ? config : {}) }
  c.repoRoot = path.resolve(String(c.repoRoot || process.cwd()))
  c.python = String(c.python || 'python')
  c.toolsDir = String(c.toolsDir || 'tools/dispatch')
  c.cdpDir = String(c.cdpDir || 'tools/cdp')
  c.allowSend = c.allowSend !== false
  c.defaultDeadlineS = Number(c.defaultDeadlineS) || 900
  c.defaultTimeoutS = Number(c.defaultTimeoutS) || 300
  c.maxOutputChars = Number(c.maxOutputChars) || 12000
  return c
}

function tail(text, n) {
  const s = String(text || '')
  return s.length > n ? s.slice(-n) : s
}

async function runPython(cfg, script, args, timeoutMs) {
  const env = { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' }
  try {
    const { stdout, stderr } = await execFileAsync(cfg.python, [script, ...args], {
      cwd: cfg.repoRoot,
      env,
      timeout: timeoutMs,
      maxBuffer: 16 * 1024 * 1024,
      windowsHide: true,
      encoding: 'utf8',
    })
    return { ok: true, code: 0, stdout: String(stdout || ''), stderr: String(stderr || '') }
  } catch (error) {
    return {
      ok: false,
      code: typeof error.code === 'number' ? error.code : -1,
      stdout: String(error.stdout || ''),
      stderr: String(error.stderr || error.message || ''),
      timedOut: Boolean(error.killed),
    }
  }
}

function scripts(cfg) {
  return {
    doctor: path.join(cfg.repoRoot, cfg.toolsDir, 'doctor.py'),
    dispatch: path.join(cfg.repoRoot, cfg.cdpDir, 'dispatch.py'),
    receipt: path.join(cfg.repoRoot, cfg.toolsDir, 'receipt.py'),
    platforms: path.join(cfg.repoRoot, cfg.toolsDir, 'platforms.json'),
  }
}

async function loadPlatforms(cfg) {
  const raw = await fs.readFile(scripts(cfg).platforms, 'utf8')
  return JSON.parse(raw).platforms || {}
}

function resultSummary(action, data) {
  if (action === 'doctor' || action === 'platforms') {
    const rows = Array.isArray(data.platforms) ? data.platforms : []
    const head = data.ready ? 'DISPATCH READY' : 'DISPATCH NOT READY'
    return [head, ...rows.map((r) => `${r.platform} ${r.status}${r.next ? ' -> ' + r.next : ''}`)].join('\n')
  }
  if (action === 'send') return data.output || (data.ok ? 'SENT' : 'SEND FAILED')
  if (action === 'wait' || action === 'confirm' || action === 'list') {
    return data.output || (data.ok ? 'OK' : 'FAILED')
  }
  return JSON.stringify(data)
}

async function executeAction(input, cfg) {
  const args = input && typeof input === 'object' ? input : {}
  const action = String(args.action || 'doctor')

  if (action === 'platforms') {
    const table = await loadPlatforms(cfg)
    return {
      ok: true,
      action,
      ready: true,
      platforms: Object.entries(table).map(([id, p]) => ({
        platform: id,
        name: p.name,
        port: p.port,
        status: p.status,
        specialties: p.specialties || [],
      })),
      summary: 'PLATFORMS ' + Object.keys(table).join(', '),
    }
  }

  if (action === 'doctor') {
    // 工具就绪口径 = 任一平台 READY（doctor 的 --any-ready）。默认全 READY 会把
    // "只有 1 个应用开着" 误报成 DISPATCH NOT READY —— 正是本工具要消灭的误报。
    const r = await runPython(cfg, scripts(cfg).doctor, ['--json', '--any-ready'], 60000)
    let parsed = null
    try {
      parsed = JSON.parse(r.stdout)
    } catch {
      parsed = null
    }
    const data = parsed || {
      ready: false,
      platforms: [],
      error: tail(r.stderr || r.stdout, 1000) || 'doctor did not return JSON',
    }
    return {
      ok: Boolean(data.ready),
      action,
      ...data,
      summary: resultSummary(action, data),
    }
  }

  if (action === 'send') {
    const platform = String(args.platform || '')
    if (!PLATFORMS.includes(platform)) throw new Error('platform must be one of: ' + PLATFORMS.join(', '))
    const text = String(args.text || '')
    if (!text.trim()) throw new Error('text is required')
    const send = args.send === true
    if (send && !cfg.allowSend) throw new Error('allowSend=false: plugin config forbids real sending')
    const table = await loadPlatforms(cfg)
    const p = table[platform]
    if (!p || !p.port) throw new Error('platform not found in platforms.json: ' + platform)
    const match = typeof args.match === 'string' && args.match ? args.match : (DEFAULT_MATCH[platform] || '')
    const timeout = Math.max(10, Number(args.timeout) || cfg.defaultTimeoutS)
    const deadline = Math.max(10, Number(args.deadline) || cfg.defaultDeadlineS)
    const expectFile = typeof args.expect_file === 'string' ? args.expect_file : ''
    const expectDir = typeof args.expect_dir === 'string' ? args.expect_dir : ''
    const marker = typeof args.marker === 'string' ? args.marker : ''
    const wantsReceipt = send && (expectFile || expectDir || marker)

    // ★ 先记台账（ts = 派发前）再发送：receipt.py 用台账 ts 当"产物必须晚于此"的基线，
    //   发送后才 log 会把本次派发自己产出的文件误判成"陈旧产物"（审计 D1）。
    let receiptId = ''
    let receiptLog = ''
    if (wantsReceipt) {
      const logArgv = ['log', '--platform', platform, '--port', String(p.port), '--task', String(args.task || text).slice(0, 80)]
      if (expectFile) logArgv.push('--expect-file', expectFile)
      if (expectDir) logArgv.push('--expect-dir', expectDir)
      if (marker) logArgv.push('--marker', marker)
      const lr = await runPython(cfg, scripts(cfg).receipt, logArgv, 60000)
      const m = /([A-Z]+-\d{8}-\d{6}(?:-\d+)?)/.exec(lr.stdout || '')
      receiptId = m ? m[1] : ''
      receiptLog = tail((lr.stdout || '') + (lr.stderr ? '\n' + lr.stderr : ''), 2000)
    }

    const argv = ['--port', String(p.port), '--text', text, '--timeout', String(timeout), '--deadline', String(deadline)]
    if (match) argv.push('--match', match)
    if (send) argv.push('--send')
    if (args.force === true) argv.push('--force')
    const r = await runPython(cfg, scripts(cfg).dispatch, argv, (deadline + 60) * 1000)
    const output = tail((r.stdout || '') + (r.stderr ? '\n' + r.stderr : ''), cfg.maxOutputChars)
    const dispatchOk = r.ok && /\bRESULT:\s*PASS\b/.test(r.stdout || '')
    const out = { ok: false, action, platform, send, output, dry_run: !send, receipt_id: receiptId, receipt_log: receiptLog }
    out.send_ok = dispatchOk

    if (wantsReceipt && !receiptId) {
      out.ok = false
      out.error = 'receipt log 未产出 id（台账没记上，产物无法判定）'
      out.summary = resultSummary(action, out)
      return out
    }

    if (!send) {
      out.ok = dispatchOk
      out.summary = resultSummary(action, out)
      return out
    }

    // 发送/预检失败（dispatch.py exit 2，未填字未发送）：台账标 FAILED，绝不留 SENT 空等。
    if (!dispatchOk) {
      if (wantsReceipt && receiptId) {
        const mr = await runPython(cfg, scripts(cfg).receipt,
          ['mark', '--id', receiptId, '--status', 'FAILED', '--note', '发送未成功（预检/点击失败），未等产物'], 60000)
        out.receipt_mark = tail((mr.stdout || '') + (mr.stderr ? '\n' + mr.stderr : ''), 2000)
      }
      out.ok = false
      out.summary = resultSummary(action, out)
      return out
    }

    // 完成只认产物：wait=true 时以产物回执为准（dispatch 的 HANG/DEADLINE 不覆盖已落盘产物）。
    if (wantsReceipt && args.wait === true) {
      const wr = await runPython(cfg, scripts(cfg).receipt,
        ['wait', '--id', receiptId, '--timeout', String(timeout), '--deadline', String(deadline)],
        (deadline + 60) * 1000)
      out.receipt_wait = tail((wr.stdout || '') + (wr.stderr ? '\n' + wr.stderr : ''), 2000)
      out.ok = /\bRESULT:\s*PASS\b/.test(wr.stdout || '')
    } else {
      out.ok = true
    }
    out.summary = resultSummary(action, out)
    return out
  }

  if (action === 'wait') {
    const argv = ['wait']
    if (args.id) argv.push('--id', String(args.id))
    // schema 暴露的是 expect_file/expect_dir；receipt.py 的 CLI 参数叫 --file/--dir。
    const file = args.file || args.expect_file
    const dir = args.dir || args.expect_dir
    if (file) argv.push('--file', String(file))
    if (dir) argv.push('--dir', String(dir))
    if (args.marker) argv.push('--marker', String(args.marker))
    if (args.port) {
      argv.push('--port', String(args.port))
    } else if (args.platform) {
      const p = (await loadPlatforms(cfg))[String(args.platform)]
      if (p && p.port) argv.push('--port', String(p.port))
    }
    argv.push('--timeout', String(Math.max(1, Number(args.timeout) || cfg.defaultTimeoutS)))
    if (args.deadline) argv.push('--deadline', String(Math.max(1, Number(args.deadline))))
    const r = await runPython(cfg, scripts(cfg).receipt, argv, (Number(args.deadline) || cfg.defaultDeadlineS) * 1000 + 60000)
    const output = tail((r.stdout || '') + (r.stderr ? '\n' + r.stderr : ''), cfg.maxOutputChars)
    return { ok: r.ok && /\bRESULT:\s*PASS\b/.test(r.stdout || ''), action, output, summary: output }
  }

  if (action === 'confirm') {
    if (!args.id) throw new Error('confirm requires id')
    const argv = ['confirm', '--id', String(args.id), '--note', String(args.note || '人工确认')]
    if (args.allow_no_artifact === true) argv.push('--allow-no-artifact')
    const r = await runPython(cfg, scripts(cfg).receipt, argv, 60000)
    const output = tail((r.stdout || '') + (r.stderr ? '\n' + r.stderr : ''), 4000)
    return { ok: r.ok && /\bRESULT:\s*PASS\b/.test(r.stdout || ''), action, output, summary: output }
  }

  if (action === 'list') {
    const argv = ['list']
    if (args.status) {
      const st = String(args.status)
      if (!STATUSES.includes(st)) throw new Error('status must be one of: ' + STATUSES.join(', '))
      argv.push('--status', st)
    }
    if (args.last) argv.push('--last', String(args.last))
    const r = await runPython(cfg, scripts(cfg).receipt, argv, 60000)
    const output = tail((r.stdout || '') + (r.stderr ? '\n' + r.stderr : ''), cfg.maxOutputChars)
    return { ok: r.ok, action, output, summary: output }
  }

  throw new Error('unknown action: ' + action)
}

export function apply(ctx, config = {}) {
  const cfg = resolveConfig(config)

  const service = {
    doctor: () => executeAction({ action: 'doctor' }, cfg),
    platforms: () => loadPlatforms(cfg),
    send: (opts = {}) => executeAction({ action: 'send', ...opts }, cfg),
    receipt: (opts = {}) => executeAction({ action: opts.action || 'wait', ...opts }, cfg),
    dispatch: (opts = {}) => executeAction(opts, cfg),
  }

  ctx.effect(() => ctx.provide('dispatch', service))

  const tool = {
    name: 'dispatch',
    description: [
      'Unified external-dispatch entry. Always start with action "doctor"; CLOSED means the app is not started, not that dispatch is impossible.',
      'action "send" fills and optionally sends a task to a platform; use send=false for dry-run.',
      'action "wait" waits for an artifact (file/dir) or marker; action "confirm" refuses a missing artifact; action "list" reads the ledger.',
      'Never treat a chat reply as completion: pass expect_file/expect_dir and wait=true.',
    ].join(' '),
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['action'],
      properties: {
        action: { type: 'string', enum: ACTIONS, description: 'doctor | platforms | send | wait | confirm | list' },
        platform: { type: 'string', enum: PLATFORMS, description: 'send target' },
        text: { type: 'string', description: 'task text (send)' },
        task: { type: 'string', description: 'short ledger task label (send)' },
        match: { type: 'string', description: 'page URL fragment; per-platform defaults live in index.js' },
        send: { type: 'boolean', description: 'true = really send; false/omitted = dry-run' },
        force: { type: 'boolean', description: 'overwrite non-empty composer' },
        expect_file: { type: 'string', description: 'artifact file to log/wait for' },
        expect_dir: { type: 'string', description: 'artifact directory to log/wait for' },
        marker: { type: 'string', description: 'reply end marker (secondary signal)' },
        wait: { type: 'boolean', description: 'after send, wait for the artifact' },
        timeout: { type: 'integer', description: 'per-wait inactivity timeout seconds' },
        deadline: { type: 'integer', description: 'overall deadline seconds' },
        id: { type: 'string', description: 'receipt id for wait/confirm' },
        note: { type: 'string', description: 'confirm note' },
        port: { type: 'integer', description: 'CDP port (wait with marker; default: from platform)' },
        status: { type: 'string', enum: STATUSES, description: 'ledger filter for list' },
        last: { type: 'integer', description: 'ledger list limit' },
        allow_no_artifact: { type: 'boolean', description: 'confirm: allow marker-only receipt after human verification' },
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
        properties: {
          summary: { type: 'string' },
          ok: { type: 'boolean' },
          action: { type: 'string' },
        },
        required: ['summary'],
      },
      render: (_args, value) => [{ type: 'text', text: String((value && value.summary) || '') }],
    },
    async execute(args) {
      return executeAction(args, cfg)
    },
  }

  ctx.effect(() => ctx.tools.register(tool))
}

export const internals = { resolveConfig, executeAction, DEFAULT_MATCH, PLATFORMS }
