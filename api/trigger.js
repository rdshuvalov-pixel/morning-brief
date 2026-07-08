// /root/morning_brief_v2/api/trigger.js
// POST /api/trigger?script=<name>&key=<shared-secret>
// Body: { "date": "YYYY-MM-DD" }  (optional, defaults to today UTC)
// Returns 200: { job_id, status, dedup }
// Returns 401 if key wrong; 400 if script unknown; 500 on Supabase error.

import { createClient } from '@supabase/supabase-js';

const ALLOWED_SCRIPTS = [
  'garmin-yesterday', 'garmin-today',
  'weather', 'calendar', 'todoist', 'food',
  'llm', 'render-publish',
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
  console.log('[trigger] debug: SUPABASE_URL present?', !!supabaseUrl, 'len=', supabaseUrl?.length, 'SERVICE_ROLE present?', !!serviceKey, 'len=', serviceKey?.length);
  if (!supabaseUrl || !serviceKey) {
    return res.status(500).json({ error: 'server misconfigured: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing' });
  }

  const sb = createClient(supabaseUrl, serviceKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  // Try insert; on unique violation (23505), return existing job
  const { data, error } = await sb
    .schema('morning_brief_v2')
    .from('jobs')
    .insert({
      script,
      status: 'pending',
      payload: { date, requested_via: 'vercel' },
    })
    .select()
    .single();

  if (!error) {
    return res.status(200).json({ job_id: data.id, status: data.status, dedup: false });
  }

  // 23505 = unique_violation (Postgres code). Supabase may wrap it; check code or message.
  const isDup = error.code === '23505'
    || (typeof error.message === 'string' && error.message.includes('jobs_dedup_idx'))
    || (typeof error.details === 'string' && error.details.includes('jobs_dedup_idx'));

  if (isDup) {
    // Fetch the existing pending/running job for this script+date
    const { data: existing, error: selErr } = await sb
      .schema('morning_brief_v2')
      .from('jobs')
      .select('id, status, triggered_at')
      .eq('script', script)
      .contains('payload', { date })
      .in('status', ['pending', 'running'])
      .order('triggered_at', { ascending: true })
      .limit(1)
      .single();

    if (selErr) {
      return res.status(500).json({ error: 'dedup lookup failed', details: selErr.message });
    }
    return res.status(200).json({
      job_id: existing.id,
      status: existing.status,
      dedup: true,
    });
  }

  return res.status(500).json({ error: 'insert failed', code: error.code, details: error.message });
}

export const config = {
  maxDuration: 10,
};
