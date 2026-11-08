# dsh-dispatch-platform

Cross-app task dispatch over CDP + **artifact-first receipts**: send a task to a local
Chromium-based AI app, then decide completion by the artifact on disk — not by the chat reply.

> Windows + DSH (DeepSeek Harness) host plugin in production; the Python core only needs
> `websockets` (stdlib otherwise) and can be used standalone.

## What it solves

- `CLOSED` means "the app is not running", not "dispatch is impossible".
- A page/window mismatch is a hard error, not a silent "done".
- A chat reply of "finished" is not a receipt: the artifact must exist, be non-empty,
  and be **newer than the dispatch time**.
- Ledger writes are cross-process locked; same-second dispatches get unique ids.

## Layout

```
tools/dispatch/         doctor.py (channel check)  receipt.py (receipt + ledger)  platforms.py (capability schema)  quota.py
tools/cdp/dispatch.py   fill -> read back -> send -> wait for reply (dry-run by default)
tools/launchers/        Windows launchers; set the corresponding *_EXE env var
plugins/dsh-dispatch/   DSH host plugin: `dispatch` tool + `ctx.get('dispatch')` service
tests/verify_v2/        checks: doctor/receipt/platform table
```

## Quick start

```powershell
pip install websockets
$env:DOUBAO_EXE = "C:\Program Files\Doubao\Doubao.exe"   # your install path
tools\launchers\start-doubao-cdp.cmd

python tools/dispatch/doctor.py           # READY / CLOSED / WRONG_WINDOW + next
python tools/cdp/dispatch.py --port 9333 --text "hello"          # dry-run
python tools/cdp/dispatch.py --port 9333 --text "hello" --send   # really send
```

Artifact-first loop (**log first, then dispatch** — the ledger timestamp is the mtime baseline):

```powershell
python tools/dispatch/receipt.py log --platform <id> --port <PORT> --task "write report" --expect-file "out\report.md"
python tools/cdp/dispatch.py --port <PORT> --text "write report to out\report.md" --send
python tools/dispatch/receipt.py wait --id <ID> --timeout 300 --deadline 900
python tools/dispatch/receipt.py confirm --id <ID> --note "reviewed"
```

- `--timeout` is the total wait unless `--deadline` is given; with `--deadline` the wait runs
  until the deadline (then `DEADLINE`, exit 4).
- Missing or stale artifact -> `confirm` refuses (exit 3).

## Platform table

`tools/dispatch/platforms.json` is a minimal routing table (id / name / port / status).
Page/selector details live in `doctor.py` and `tools/cdp/dispatch.py`. Optional work-mode and
model-precheck policies are supported by `platforms.py`; leave them unset unless you need them.

| id | name | port |
|---|---|---|
| qwen | Qwen Work | 9555 |
| doubao | Doubao | 9333 |
| dumate | DuMate | 9666 |
| trae | Trae CN | 9444 |
| workbuddy | WorkBuddy | 9222 |

## Checks (fail-able, not "file exists")

```powershell
python tests/verify_v2/check_dispatch_capability.py
python tests/verify_v2/check_receipt_retry.py
python tests/verify_v2/check_dispatch_wait.py
python tests/verify_v2/check_platforms_table.py
node plugins/dsh-dispatch/test-internals.mjs
```

## DSH plugin (optional)

`plugins/dsh-dispatch` exposes the `dispatch(action=doctor|send|wait|confirm|list|platforms)` tool
and the `ctx.get('dispatch')` service. Configure via `cordis.patch.yml`; `repoRoot`/`python`
default to the process environment and can be overridden.

## Design rules

- Dry-run by default: no `--send` = fill, read back, clear (zero side effects).
- No token/cookie/raw URL in output (pages are reduced to a file name / host + path tail).
- Fail loud instead of silently degrading.
- Ledger is append-mostly: original entries are never rewritten; retries append `-R1/-R2`
  and escalate to `-ESC` past the limit.

## License

MIT. No data is collected; runtime files (ledger, quota) are gitignored.
