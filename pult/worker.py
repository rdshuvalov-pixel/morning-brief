#!/usr/bin/env python3
"""
pult-worker: claim jobs from Supabase, run morning-brief scripts, update status.

Architecture (per plan v2: 2026-07-08):
  - Vercel static /pult + Vercel Functions (api/trigger.js, api/status.js)
  - Vercel Function inserts row into morning_brief_v2.jobs (status=pending)
  - THIS worker (mbrief-pult-worker.service) loops every 10s, claims via RPC
  - Runs the corresponding bash script (or generate_llm.py for 'llm')
  - Updates status to done|failed|orphaned

Auth: this worker uses SUPABASE_KEY (anon) — RPCs are SECURITY DEFINER.
Service role key is NOT used here; only Vercel Functions use it.

Pitfalls honored:
  §7: scripts hard-code today — we only pass date, scripts use date if accepted
  §18: cron disabled, manual-only — this is the manual-mode runner
  §24a: fetch_garmin_today.sh writes all fields (incl. morning settled)
  §29: archive_and_publish.sh may take 180-300s — we set timeout=600
  §36a: not relevant here (no git operations)
"""
import os
import sys
import signal
import subprocess
import time
import json
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

load_dotenv('/root/morning_brief_v2/.env')

SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_KEY = os.environ['SUPABASE_KEY']
PROJECT_ROOT = '/root/morning_brief_v2'

# Map: script-name -> shell command (no date — scripts compute it themselves)
# LLM scripts run via venv python (system python lacks deps → ModuleNotFoundError).
SCRIPTS = {
    'garmin-yesterday': ['bash', f'{PROJECT_ROOT}/scripts/manual/fetch_garmin_yesterday.sh'],
    'garmin-today':     ['bash', f'{PROJECT_ROOT}/scripts/manual/fetch_garmin_today.sh'],
    'weather':          ['bash', f'{PROJECT_ROOT}/scripts/manual/fetch_weather.sh'],
    'calendar':         ['bash', f'{PROJECT_ROOT}/scripts/manual/fetch_calendar.sh'],
    'todoist':          ['bash', f'{PROJECT_ROOT}/scripts/manual/fetch_todoist.sh'],
    'food':             ['bash', f'{PROJECT_ROOT}/scripts/manual/fetch_food.sh'],
    'llm':              [f'{PROJECT_ROOT}/.venv/bin/python', f'{PROJECT_ROOT}/scripts/manual/generate_llm.py'],
    # --date is appended at run-time in run_one() (since 2026-07-09 — uses
    # payload.date which the Vercel Function populates with today-utc).
    # --write is intentionally OMITTED: worker runs in dry-run mode so we
    # generate the payload, expose it on stdout, but do NOT auto-publish.
    # Manual backfills use `generate_llm.py --date YYYY-MM-DD --write` directly
    # (the operator gates the publish step). Without --date, argparse fails
    # with "the following arguments are required: --date" → rc=2, which is
    # exactly the symptom we saw on 2026-07-17 jobs 151..181 in journalctl.
    # Earlier versions of the SCRIPTS dict had `--write` here — that was
    # the upstream bug; ensure future edits keep both flags out.
    'weekly-recap':     [f'{PROJECT_ROOT}/.venv/bin/python', f'{PROJECT_ROOT}/scripts/manual/generate_weekly_recap.py'],
    'render-publish':   ['bash', f'{PROJECT_ROOT}/scripts/manual/archive_and_publish.sh'],
    # Per-block LLM narratives (миграция 007, 2026-07-17): одна кнопка = один блок.
    # Тонкая развязка: можно регенерировать погоду отдельно, не гоняя 5 блоков.
    # --write НЕ передаём (mirror 'llm' rule above — worker dry-run).
    # Operator gates the publish with `--write` explicitly.
    'llm-block-weather':   [f'{PROJECT_ROOT}/.venv/bin/python', f'{PROJECT_ROOT}/scripts/manual/generate_llm_blocks.py'],
    'llm-block-tasks':     [f'{PROJECT_ROOT}/.venv/bin/python', f'{PROJECT_ROOT}/scripts/manual/generate_llm_blocks.py'],
    'llm-block-movement':  [f'{PROJECT_ROOT}/.venv/bin/python', f'{PROJECT_ROOT}/scripts/manual/generate_llm_blocks.py'],
    'llm-block-calendar':  [f'{PROJECT_ROOT}/.venv/bin/python', f'{PROJECT_ROOT}/scripts/manual/generate_llm_blocks.py'],
    'llm-block-battery':   [f'{PROJECT_ROOT}/.venv/bin/python', f'{PROJECT_ROOT}/scripts/manual/generate_llm_blocks.py'],
    'llm-blocks-all':      [f'{PROJECT_ROOT}/.venv/bin/python', f'{PROJECT_ROOT}/scripts/manual/generate_llm_blocks.py'],
}

# Per-script timeout in seconds.
# Calendar gets 120s (Google Calendar API is slow on cold start; was timing out at 60s
# when fetched via worker, but runs in ~9s when invoked manually — see diag 2026-07-09).
TIMEOUTS = {
    'garmin-yesterday': 60,
    'garmin-today':     60,
    'weather':          60,
    'calendar':         120,  # was 60; bumped because Google API slow on cold start
    'todoist':          60,
    'food':             60,
    'llm':              300,  # LLM compose (120s timeout) + 5 opinions (60s each, parallel) → ~180s; headroom=300
    'weekly-recap':     240,  # 4× Supabase queries + LLM + Telegram send
    'render-publish':   600,  # archive_and_publish.sh full pipeline
    # Per-block LLM narratives (миграция 007). Каждый блок — отдельный hermes -z.
    # Single: 90s (1 hermes call + headroom). All: 300s (5 sequential calls × ~30s + headroom).
    'llm-block-weather':   90,
    'llm-block-tasks':     90,
    'llm-block-movement':  90,
    'llm-block-calendar':  90,
    'llm-block-battery':   90,
    'llm-blocks-all':     300,
}

# Reaper threshold: running jobs older than this are marked orphaned
REAPER_THRESHOLD_SEC = 15 * 60
REAPER_INTERVAL_SEC = 60
POLL_INTERVAL_SEC = 10

sb = create_client(SUPABASE_URL, SUPABASE_KEY)
_running = False  # set while a job is being processed (for SIGTERM handler)


def claim() -> dict | None:
    """Atomically claim the oldest pending job via RPC.

    The public.claim_next_job() RPC was rewritten 2026-07-08 to return jsonb
    (a single dict) instead of morning_brief_v2.jobs record. This sidesteps
    PostgREST's type-cache issue (Pitfall §3 + new §38 in morning-brief-v2 skill).

    On 2026-07-17 the RPC was observed returning a *list of dicts* on some
    calls (probably when SETOF jsonb lands as a single-row array). Handle
    all three shapes: dict, list[dict], str — pick the first dict or fall
    through to None.
    """
    try:
        r = sb.rpc('claim_next_job').execute()
        if r.data is None or r.data == '':
            return None
        # r.data may be: str (jsonb-as-text), dict, list[dict]
        if isinstance(r.data, str):
            parsed = json.loads(r.data)
        else:
            parsed = r.data
        if isinstance(parsed, list):
            return parsed[0] if parsed else None
        if isinstance(parsed, dict):
            return parsed
        return None
    except Exception as e:
        print(f'[claim] error: {e}', flush=True)
        return None


def update_status(job_id: int, status: str, error: str | None = None) -> None:
    """Update job status, error, finished_at via SECURITY DEFINER RPC.

    Bypasses supabase-js .schema() routing bug (Pitfall §38) by using
    update_job_status() RPC that updates from postgres role inside the function.
    """
    try:
        sb.rpc('update_job_status', {
            'p_id': job_id,
            'p_status': status,
            'p_error': error,
        }).execute()
    except Exception as e:
        print(f'[update_status {job_id}] error: {e}', flush=True)


def run_one(job: dict) -> None:
    """Process one job: run script, capture result, update status.

    job shape (verified 2026-07-17 against claim_next_job RPC):
      { "id": int, "script": str, "status": str,
        "payload": [ {date: ...}, "extra-json-str" ],
        "triggered_at": "...",
        ... }
    The payload field is a JSONB array, not a dict. Earlier code assumed a
    dict and crashed on this morning's session with
        AttributeError: 'list' object has no attribute 'get'
    (rc=2 cascades because the loop dies — job rows never get update_status,
    leaving them in 'running' until the 15-min reaper marks them orphaned).
    """
    job_id = job['id']
    script = job['script']
    payload = job.get('payload') or []
    # payload may be [] OR [args1_dict, args2_str] OR [args1_dict] depending
    # on insert_job args. First element is the merged payload JSON, second
    # (if present) is the p_payload_extra string. Pull `date` from the first
    # element if it's a dict, else default to 'unknown'.
    date = 'unknown'
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        date = payload[0].get('date') or 'unknown'
    print(f'[run_one] job={job_id} script={script} date={date}', flush=True)

    if script not in SCRIPTS:
        update_status(job_id, 'failed', f'unknown script: {script}')
        return

    # Mark running (also via RPC to bypass .schema() bug)
    try:
        sb.rpc('update_job_status', {
            'p_id': job_id,
            'p_status': 'running',
            'p_error': None,
        }).execute()
    except Exception as e:
        print(f'[mark running] error: {e}', flush=True)
        return  # leave as pending; will be retried next loop

    cmd = SCRIPTS[script]
    timeout = TIMEOUTS[script]

    # LLM script also needs --date (already added 'llm' as full venv python path,
    # but date arg comes after the script path)
    if script == 'llm':
        cmd = list(cmd) + ['--date', date]

    # Per-block LLM narratives: --date + either --block <name> or --all.
    # ВАЖНО: --write НЕ передаём — worker dry-run по аналогии с 'llm' (см. SCRIPTS comment).
    if script.startswith('llm-block-') or script == 'llm-blocks-all':
        if script == 'llm-blocks-all':
            cmd = list(cmd) + ['--date', date, '--all']
        else:
            # 'llm-block-weather' → 'weather'
            block_name = script[len('llm-block-'):]
            cmd = list(cmd) + ['--date', date, '--block', block_name]

    # Build subprocess env. Preserve worker's env (loads .env via systemd EnvironmentFile),
    # but ensure PATH includes Hermes-agent venv bin dir so scripts can find gws-cli
    # (used by CalendarProvider). This belt-and-suspenders against future changes to
    # systemd unit's PATH.
    HERMES_BIN_DIR = '/usr/local/lib/hermes-agent/venv/bin'
    env = os.environ.copy()
    if HERMES_BIN_DIR not in env.get('PATH', '').split(':'):
        env['PATH'] = f"{HERMES_BIN_DIR}:{env.get('PATH', '')}"
    env['PYTHONUNBUFFERED'] = '1'
    # gws-cli OAuth handshake needs TERM to determine TTY mode; without it
    # gws-cli blocks on OAuth URL display. Set TERM=dumb (non-interactive).
    env['TERM'] = 'dumb'

    # Special case: weekly-recap AND llm scripts invoke `hermes -z` which
    # keys off $HOME for picking the LLM provider config. When HOME=/root
    # (interactive shell) hermes finds /root/.hermes/.env with MINIMAX_API_KEY.
    # When HOME=/root/.hermes/profiles/developer/home (this worker's systemd
    # unit) that path's .hermes/.env doesn't exist and hermes errors
    # "No inference provider configured" (2026-07-10). Force HOME=/root for
    # any script that runs hermes -z — the unit HOME stays for gws-cli OAuth
    # in the provider scripts (fetch_calendar, fetch_todoist).
    if script in ('llm', 'weekly-recap'):
        env['HOME'] = '/root'

    # DIAG (2026-07-09): print env that subprocess gets — debugging calendar hang
    print(f'[run_one] job={job_id} env dump:', flush=True)
    print(f'  PATH={env.get("PATH")}', flush=True)
    print(f'  HOME={env.get("HOME")}', flush=True)
    print(f'  LANG={env.get("LANG")}', flush=True)
    print(f'  SHELL={env.get("SHELL")}', flush=True)
    print(f'  TERM={env.get("TERM")}', flush=True)
    print(f'  PWD={env.get("PWD")}', flush=True)

    # Run in its own process group so we can SIGKILL the whole tree on timeout.
    # Without this, gws-cli (spawned by python -> asyncio.create_subprocess_exec)
    # survives as an orphan and competes with the next click for the same
    # network/cred handles. Setsid also detaches from the worker's controlling tty.
    # We use Popen() + communicate(timeout=...) explicitly so `proc` is ALWAYS bound
    # in the TimeoutExpired handler — earlier we used subprocess.run(timeout=...) and
    # the handler raised UnboundLocalError on `proc`, which left the hang un-killed
    # and the job stuck in 'running' forever (2026-07-09).
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=PROJECT_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # POSIX setsid: child gets its own pgid
        )
    except Exception as e:
        update_status(job_id, 'failed', f'popen failed: {type(e).__name__}: {e}')
        print(f'[run_one] job={job_id} POPEN FAIL {e}', flush=True)
        return

    timed_out = False
    error_msg: str | None = None
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            rc = proc.returncode
            stderr_text = stderr.decode('utf-8', errors='replace')[:2000]
            stdout_tail = stdout.decode('utf-8', errors='replace')[-500:]

            if rc == 0:
                update_status(job_id, 'done')
                print(f'[run_one] job={job_id} DONE', flush=True)
            else:
                # DIAG 2026-07-17 — show ALL stderr not just last 200 chars
                # so we stop guessing. Also print stderr line count and
                # last INFO/ERROR/WARNING level lines.
                err_lines = stderr_text.splitlines()
                err = (
                    f'rc={rc} stderr_lines={len(err_lines)} '
                    f'last300={stderr_text[-300:]} '
                    f'last_errlines='
                    f'{[l for l in err_lines[-12:] if "ERROR" in l or "Traceback" in l or "return 2" in l or "None" in l]}'
                )
                update_status(job_id, 'failed', err)
                print(f'[run_one] job={job_id} FAILED rc={rc} stderr_tail={stderr_text[-300:]}', flush=True)
        except subprocess.TimeoutExpired:
            timed_out = True
            # `proc` is bound here because we assigned it before communicate().
            # SIGKILL the entire process group (bash -> python -> gws-cli -> anything else
            # spawned by `cmd`) so no grandchildren survive as orphans. proc.pid is the
            # bash leader; its pgid == pid because of start_new_session=True above.
            pgid = os.getpgid(proc.pid)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError) as kerr:
                # Group already gone, or we don't own it — fall back to proc.kill().
                print(f'[run_one] job={job_id} killpg fallback: {kerr}', flush=True)
                try:
                    proc.kill()
                except Exception:
                    pass
            # Drain pipes so the killed child actually exits and is reaped.
            try:
                proc.communicate(timeout=10)
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            update_status(job_id, 'failed', f'timeout after {timeout}s (group killed)')
            print(f'[run_one] job={job_id} TIMEOUT after {timeout}s (group killed)', flush=True)
        except Exception as e:
            error_msg = f'{type(e).__name__}: {e}'
            tb = traceback.format_exc()[:500]
            update_status(job_id, 'failed', f'{error_msg}\n{tb}')
            print(f'[run_one] job={job_id} ERROR {e}', flush=True)
    finally:
        # Safety net: if anything went sideways and `proc` is still alive (e.g.,
        # Popen succeeded but communicate raised before we could call killpg),
        # make sure the entire group is dead so the worker never leaks a hanging
        # grandchild into the next job.
        try:
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
                if not timed_out:
                    print(f'[run_one] job={job_id} finally-kill (proc still alive)', flush=True)
        except Exception:
            pass


def reaper() -> None:
    """Mark running jobs older than REAPER_THRESHOLD_SEC as orphaned."""
    try:
        r = sb.rpc('reap_stuck_jobs').execute()
        if r.data and r.data > 0:
            print(f'[reaper] orphaned {r.data} stuck job(s)', flush=True)
    except Exception as e:
        print(f'[reaper] error: {e}', flush=True)


def on_sigterm(sig, frame):
    """If a job is running, let it finish; if idle, exit cleanly."""
    if _running:
        print('SIGTERM during running job — will exit after finish', flush=True)
        # Don't sys.exit here; let the current job finish, then loop sees it
        # and exits on next iteration. Setting a flag would be cleaner but
        # we keep it simple: next loop tick checks _running, if False, exits.
    else:
        print('SIGTERM — exiting', flush=True)
        sys.exit(0)


def main() -> int:
    global _running
    signal.signal(signal.SIGTERM, on_sigterm)
    signal.signal(signal.SIGINT, on_sigterm)

    print(f'pult-worker started (pid={os.getpid()}, poll={POLL_INTERVAL_SEC}s, reaper={REAPER_INTERVAL_SEC}s)', flush=True)
    last_reap = 0.0

    while True:
        _running = True
        try:
            job = claim()
            if job:
                run_one(job)
            else:
                # No pending jobs: sleep, occasionally reap
                _running = False
                if time.time() - last_reap > REAPER_INTERVAL_SEC:
                    reaper()
                    last_reap = time.time()
                time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            print('KeyboardInterrupt — exiting', flush=True)
            return 0
        except Exception as e:
            _running = False
            print(f'[loop] error: {e}', flush=True)
            traceback.print_exc()
            time.sleep(POLL_INTERVAL_SEC)


if __name__ == '__main__':
    sys.exit(main())
