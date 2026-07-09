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
    'llm':              [f'{PROJECT_ROOT}/.venv/bin/python', f'{PROJECT_ROOT}/scripts/manual/generate_llm.py', '--write'],
    'render-publish':   ['bash', f'{PROJECT_ROOT}/scripts/manual/archive_and_publish.sh'],
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
    'llm':              180,  # LLM is slow (Pitfall §7: 45s timeout in hermes -z)
    'render-publish':   600,  # archive_and_publish.sh full pipeline
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
    """
    try:
        r = sb.rpc('claim_next_job').execute()
        if r.data is None or r.data == '':
            return None
        # r.data is a jsonb string OR a dict depending on supabase-py version
        if isinstance(r.data, str):
            return json.loads(r.data)
        return r.data
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
    """Process one job: run script, capture result, update status."""
    job_id = job['id']
    script = job['script']
    date = (job.get('payload') or {}).get('date', 'unknown')
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

    env = {**os.environ, 'PYTHONUNBUFFERED': '1'}

    try:
        proc = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            timeout=timeout,
        )
        rc = proc.returncode
        stderr = proc.stderr.decode('utf-8', errors='replace')[:2000]
        stdout_tail = proc.stdout.decode('utf-8', errors='replace')[-500:]

        if rc == 0:
            update_status(job_id, 'done')
            print(f'[run_one] job={job_id} DONE', flush=True)
        else:
            err = f'rc={rc} stderr={stderr[:200]}'
            update_status(job_id, 'failed', err)
            print(f'[run_one] job={job_id} FAILED {err[:200]}', flush=True)
    except subprocess.TimeoutExpired:
        update_status(job_id, 'failed', f'timeout after {timeout}s')
        print(f'[run_one] job={job_id} TIMEOUT after {timeout}s', flush=True)
    except Exception as e:
        tb = traceback.format_exc()[:500]
        update_status(job_id, 'failed', f'{type(e).__name__}: {e}\n{tb}')
        print(f'[run_one] job={job_id} ERROR {e}', flush=True)


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
