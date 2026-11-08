import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { apply, internals } from './index.js'

const cfg = internals.resolveConfig({
  repoRoot: process.env.DISPATCH_REPO_ROOT || process.cwd(),
  python: process.env.DISPATCH_PY || 'python',
})

// 1) pure action paths
const platforms = await internals.executeAction({ action: 'platforms' }, cfg)
assert.equal(platforms.ok, true)
assert.equal(platforms.platforms.length, 5)

// 2) fake Host context: plugin must provide the dispatch Service and register the tool
const provided = {}
let registered = null
const ctx = {
  provided,
  effect(fn) { return fn() },
  provide(name, value) { provided[name] = value; return () => { delete provided[name] } },
  get(name) { return provided[name] },
  tools: { register(tool) { registered = tool; return () => { registered = null } } },
}
apply(ctx, { ...cfg })
assert.ok(provided.dispatch, 'dispatch Service must be provided')
assert.ok(registered && registered.name === 'dispatch', 'dispatch tool must be registered')

// 3) context-manager-style call: ctx.get('dispatch').doctor()
const doctor = await ctx.get('dispatch').doctor()
assert.ok(Array.isArray(doctor.platforms))
assert.equal(doctor.platforms.length, 5)
assert.equal(typeof doctor.summary, 'string')

// 3b) 工具就绪口径：doctor 必须带 --any-ready。默认"全部 READY"会把
//     "只有 1 个应用开着" 误报成 DISPATCH NOT READY（本工具存在的意义就是消灭这种误报）。
const td = fs.mkdtempSync(path.join(os.tmpdir(), 'dispatch-stub-'))
try {
  const stubDir = path.join(td, 'tools', 'dispatch')
  fs.mkdirSync(stubDir, { recursive: true })
  const stub = path.join(stubDir, 'doctor.py')
  fs.writeFileSync(stub, [
    'import json, sys',
    "open(__file__ + '.args', 'w', encoding='utf-8').write(' '.join(sys.argv[1:]))",
    'print(json.dumps({"ready": True, "all_ready": False, "platforms": [{"platform": "qwen", "status": "READY", "next": ""}]}, ensure_ascii=False))',
  ].join('\n'), 'utf8')
  const stubCfg = internals.resolveConfig({ repoRoot: td, python: cfg.python, toolsDir: 'tools/dispatch' })
  const dr = await internals.executeAction({ action: 'doctor' }, stubCfg)
  const recorded = fs.readFileSync(stub + '.args', 'utf8')
  assert.ok(recorded.includes('--any-ready'), 'doctor must pass --any-ready (any platform ready = tool ready)')
  assert.equal(dr.ok, true)
} finally {
  fs.rmSync(td, { recursive: true, force: true })
}

// 3c) send 时序：先记台账（ts=派发前）再发送；发送失败标 FAILED 且不空等产物。
//     发送后才 log 会让 receipt.py 把本次产物判成"陈旧产物"（审计 D1）。
const td2 = fs.mkdtempSync(path.join(os.tmpdir(), 'dispatch-send-'))
try {
  fs.mkdirSync(path.join(td2, 'tools', 'dispatch'), { recursive: true })
  fs.mkdirSync(path.join(td2, 'tools', 'cdp'), { recursive: true })
  fs.writeFileSync(path.join(td2, 'tools', 'dispatch', 'platforms.json'),
    JSON.stringify({ platforms: { qwen: { name: 'q', port: 9555 } } }))
  const calls = path.join(td2, 'calls.jsonl')
  const rec = [
    'import json, os, sys',
    "with open(os.environ['STUB_CALLS'], 'a', encoding='utf-8') as f:",
    "    f.write(json.dumps(sys.argv[1:], ensure_ascii=False) + '\\n')",
  ].join('\n') + '\n'
  fs.writeFileSync(path.join(td2, 'tools', 'dispatch', 'receipt.py'), rec + [
    "if sys.argv[1] == 'log':",
    "    print('[台账] 已记 FIX-20260101-000000（qwen → t）')",
    "print('RESULT: PASS')",
  ].join('\n') + '\n')
  fs.writeFileSync(path.join(td2, 'tools', 'cdp', 'dispatch.py'), rec + [
    "if os.environ.get('STUB_DISPATCH_FAIL') == '1':",
    "    print('RESULT: FAIL')",
    '    sys.exit(1)',
    "print('RESULT: PASS')",
  ].join('\n') + '\n')

  const sendCfg = internals.resolveConfig({ repoRoot: td2, python: cfg.python, toolsDir: 'tools/dispatch', cdpDir: 'tools/cdp' })
  process.env.STUB_CALLS = calls
  const okRes = await internals.executeAction(
    { action: 'send', platform: 'qwen', text: 'hi', send: true, expect_file: 'out.md', wait: true }, sendCfg)
  const calls1 = fs.readFileSync(calls, 'utf8').trim().split('\n').map((x) => JSON.parse(x))
  const idxLog = calls1.findIndex((x) => x[0] === 'log')
  const idxDispatch = calls1.findIndex((x) => x.includes('--text'))
  const idxWait = calls1.findIndex((x) => x[0] === 'wait')
  assert.ok(idxLog >= 0 && idxDispatch > idxLog, 'receipt log must come BEFORE dispatch (stale-artifact anchor)')
  assert.ok(idxWait > idxDispatch, 'receipt wait must come AFTER dispatch')
  assert.equal(okRes.ok, true)

  fs.writeFileSync(calls, '')
  process.env.STUB_DISPATCH_FAIL = '1'
  const badRes = await internals.executeAction(
    { action: 'send', platform: 'qwen', text: 'hi', send: true, expect_file: 'out.md', wait: true }, sendCfg)
  const calls2 = fs.readFileSync(calls, 'utf8').trim().split('\n').map((x) => JSON.parse(x))
  assert.equal(badRes.ok, false)
  assert.ok(calls2.some((x) => x[0] === 'mark' && x.includes('FAILED')), 'failed send must mark receipt FAILED')
  assert.ok(!calls2.some((x) => x[0] === 'wait'), 'failed send must not wait for artifact')
  delete process.env.STUB_DISPATCH_FAIL
} finally {
  delete process.env.STUB_CALLS
  fs.rmSync(td2, { recursive: true, force: true })
}

// 4) schema is model-facing and action enum is complete
assert.deepEqual(registered.parameters.properties.action.enum.sort(), ['confirm', 'doctor', 'list', 'platforms', 'send', 'wait'])

console.log('DISPATCH_PLUGIN_SELFTEST PASS')
