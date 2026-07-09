// /root/morning_brief_v2/api/trigger.js
// POST /api/trigger?script=<name>&key=<shared-secret>
// Body: { "date": "YYYY-MM-DD" }  (optional, defaults to today UTC)
//
// Architecture: Uses SECURITY DEFINER RPC public.insert_job() instead of
// direct .from('jobs').insert() because supabase-js .schema() doesn't
// properly route INSERTs through the Accept-Profile header in this Supabase
// setup — it lands on public.jobs (which doesn't exist) and returns 42501.
//
// Returns 200: { job_id, status, dedup }
// Returns 401 if key wrong; 400 if script unknown; 500 on Supabase error.

import { createClient } from '@supabase/supabase-js';

const ALLOWED_SCRIPTS = [
  'garmin-yesterday', 'garmin-today',
  'weather', 'calendar', 'todoist', 'food',
  'llm', 'weekly-recap',
  'render-publish',
];

function todayUtc() {
  return new Date().toISOString().slice(0, 10);
}

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'method not allowed' });
  }

  const { script, key } = req.query;
  if (!key || key !== process.env.PULT_SHARED_SECRET) {
    return res.status(401).json({ error: 'unauthorized' });
  }
  if (!script || !ALLOWED_SCRIPTS.includes(script)) {
    return res.status(400).json({ error: 'unknown script', allowed: ALLOWED_SCRIPTS });
  }

  const body = (req.body && typeof req.body === 'object') ? req.body : {};
  const date = body.date || todayUtc();

  const supabaseUrl = process.env.SUPABASE_URL;
  const serviceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!supabaseUrl || !serviceKey) {
    return res.status(500).json({ error: 'server misconfigured: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing' });
  }

  const sb = createClient(supabaseUrl, serviceKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  // Call SECURITY DEFINER RPC that INSERTs inside the function
  // (bypasses the supabase-js .schema() routing bug).
  const { data, error } = await sb.rpc('insert_job', {
    p_script: script,
    p_date: date,
    p_payload_extra: { requested_via: 'vercel' },
  });

  if (!error) {
    return res.status(200).json(data);
  }

  // Diagnostic: return full error to browser as text/plain for easy reading
  const textBody = `TRIGGER FAILED
================
supabase_code:    ${error.code || '(none)'}
supabase_message: ${error.message || '(none)'}
supabase_details: ${error.details || '(none)'}
supabase_hint:    ${error.hint || '(none)'}
================
ENV CHECK:
SUPABASE_URL_len:     ${supabaseUrl?.length || 0}
SERVICE_ROLE_len:     ${serviceKey?.length || 0}
`;
  const accept = req.headers.accept || '';
  if (accept.includes('text/html')) {
    res.setHeader('Content-Type', 'text/plain; charset=utf-8');
    return res.status(500).send(textBody);
  }
  return res.status(500).json({
    error: 'insert_job rpc failed',
    supabase_code: error.code,
    supabase_message: error.message,
    supabase_details: error.details,
    supabase_hint: error.hint,
    env_check: {
      SUPABASE_URL_len: supabaseUrl?.length || 0,
      SERVICE_ROLE_len: serviceKey?.length || 0,
    },
  });
}

export const config = {
  maxDuration: 10,
};