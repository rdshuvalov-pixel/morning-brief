// /root/morning_brief_v2/web/pult/pult.js
// Client-side: 8 buttons, POST /api/trigger, GET /api/status polling, UI block 3s.
// Auth: shared secret in URL ?key= or in sessionStorage (set via input above).

(() => {
  'use strict';

  const API_BASE = '';  // same origin
  const POLL_MS = 2000;
  const POLL_TIMEOUT_MS = 600000;  // 10 min
  const UI_BLOCK_MS = 3000;
  const sessionKey = 'pult_secret';

  const BUTTONS = [
    { id: 'garmin-yesterday', label: 'Garmin (вчера)',     script: 'garmin-yesterday' },
    { id: 'garmin-today',     label: 'Garmin (сегодня)',   script: 'garmin-today' },
    { id: 'weather',          label: 'Погода',             script: 'weather' },
    { id: 'calendar',         label: 'Календарь',          script: 'calendar' },
    { id: 'todoist',          label: 'Задачи (Todoist)',   script: 'todoist' },
    { id: 'food',             label: 'Еда',                script: 'food' },
    { id: 'llm',              label: 'LLM-нарратив',       script: 'llm' },
    { id: 'render-publish',   label: 'Render + publish',   script: 'render-publish' },
  ];

  // ---- Secret management ----
  function getSecret() {
    const fromUrl = new URLSearchParams(location.search).get('key');
    if (fromUrl) {
      sessionStorage.setItem(sessionKey, fromUrl);
      // Clean URL to avoid leaking key into history
      const clean = location.pathname;
      history.replaceState(null, '', clean);
    }
    return sessionStorage.getItem(sessionKey) || '';
  }

  function setAuthState() {
    const el = document.getElementById('auth-state');
    const sec = getSecret();
    if (sec) {
      el.textContent = 'key set';
      el.className = 'auth-state auth-ok';
    } else {
      el.textContent = 'no key';
      el.className = 'auth-state auth-absent';
    }
  }

  // ---- Date (Europe/Lisbon) ----
  function todayLisbon() {
    return new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Europe/Lisbon',
      year: 'numeric', month: '2-digit', day: '2-digit',
    }).format(new Date());
  }

  // ---- Render ----
  function renderButtons() {
    const root = document.getElementById('buttons');
    root.innerHTML = '';
    for (const b of BUTTONS) {
      const btn = document.createElement('button');
      btn.id = `btn-${b.id}`;
      btn.className = 'pult-btn';
      btn.dataset.script = b.script;
      btn.textContent = b.label;
      btn.addEventListener('click', () => onClick(b));
      root.appendChild(btn);
    }
  }

  // ---- UI block 3s + click handler ----
  const blocked = new Set();
  async function onClick(b) {
    if (blocked.has(b.id)) return;
    blocked.add(b.id);
    setTimeout(() => blocked.delete(b.id), UI_BLOCK_MS);

    const btn = document.getElementById(`btn-${b.id}`);
    btn.disabled = true;
    btn.classList.add('running');
    btn.classList.remove('done', 'failed', 'orphaned');

    const secret = getSecret();
    if (!secret) {
      showBanner('error', 'нет ключа — введите в поле выше');
      finish(btn, 'failed');
      return;
    }

    try {
      const r = await fetch(
        `${API_BASE}/api/trigger?script=${encodeURIComponent(b.script)}&key=${encodeURIComponent(secret)}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ date: todayLisbon() }),
        }
      );
      const json = await r.json().catch(() => ({}));
      if (!r.ok) {
        const msg = json.error || `HTTP ${r.status}`;
        showBanner('error', `${b.label} ✗ ${msg}`);
        finish(btn, 'failed');
        return;
      }
      if (json.dedup) {
        showBanner('info', `${b.label}: уже в работе, job #${json.job_id} (${json.status})`);
      } else {
        showBanner('info', `${b.label}: запущено, job #${json.job_id}`);
      }
      poll(b, json.job_id);
    } catch (e) {
      showBanner('error', `${b.label} ✗ ${e.message}`);
      finish(btn, 'failed');
    }
  }

  function finish(btn, status) {
    btn.disabled = false;
    btn.classList.remove('running');
    if (status) btn.classList.add(status);
    if (status === 'done') setTimeout(() => btn.classList.remove('done'), 5000);
    if (status === 'failed' || status === 'orphaned') setTimeout(() => btn.classList.remove(status), 8000);
  }

  // ---- Poll /api/status ----
  async function poll(b, jobId) {
    const btn = document.getElementById(`btn-${b.id}`);
    const started = Date.now();
    const secret = getSecret();

    const tick = async () => {
      if (Date.now() - started > POLL_TIMEOUT_MS) {
        showBanner('error', `${b.label} ✗ timeout 10m`);
        finish(btn, 'failed');
        return;
      }
      try {
        const r = await fetch(
          `${API_BASE}/api/status?id=${jobId}&key=${encodeURIComponent(secret)}`
        );
        const json = await r.json().catch(() => ({}));
        if (!r.ok) {
          // transient error — keep polling
          return;
        }
        const st = json.status;
        if (st === 'done') {
          showBanner('ok', `${b.label} ✓ ок (${Math.round((Date.now()-started)/1000)}s)`);
          finish(btn, 'done');
          return;
        }
        if (st === 'failed') {
          const err = (json.error || '').slice(0, 100);
          showBanner('error', `${b.label} ✗ ${err}`);
          finish(btn, 'failed');
          return;
        }
        if (st === 'orphaned') {
          showBanner('error', `${b.label} ✗ orphaned (>15m running)`);
          finish(btn, 'orphaned');
          return;
        }
        // pending | running — keep polling
      } catch (e) {
        // network blip — keep polling
      }
      setTimeout(tick, POLL_MS);
    };
    setTimeout(tick, POLL_MS);
  }

  // ---- Banner ----
  function showBanner(kind, msg) {
    const b = document.getElementById('status-banner');
    b.className = `banner banner-${kind}`;
    b.textContent = msg;
    b.hidden = false;
    if (kind === 'ok') setTimeout(() => (b.hidden = true), 5000);
  }

  // ---- Init ----
  document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('today-date').textContent = todayLisbon();
    setAuthState();
    renderButtons();
    document.getElementById('key-save').addEventListener('click', () => {
      const v = document.getElementById('key-input').value.trim();
      if (v) {
        sessionStorage.setItem(sessionKey, v);
        document.getElementById('key-input').value = '';
        setAuthState();
        showBanner('info', 'ключ сохранён в sessionStorage');
      }
    });
  });
})();
