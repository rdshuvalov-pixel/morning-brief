// /root/morning_brief_v2/api/diag.js
// GET /api/diag?key=SHARED_SECRET
// Returns: env_check, then tries SELECT, INSERT, UPDATE on morning_brief_v2.jobs.
// This isolates WHICH permission service_role is missing.

import { createClient } from '@supabase/supabase-js';

export default async function handler(req, res) {
  const { key } = req.query;
  if (key !== process.env.PULT_SHARED_SECRET) {
    return res.status(401).json({ error: 'unauthorized' });
  }

  const supabaseUrl = process.env.SUPABASE_URL;
  const serviceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!supabaseUrl || !serviceKey) {
    return res.status(500).json({ error: 'env missing' });
  }

  const sb = createClient(supabaseUrl, serviceKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  const out = {
    env_check: { SUPABASE_URL_len: supabaseUrl.length, SERVICE_ROLE_len: serviceKey.length },
  };

  // 1) SELECT (read)
  try {
    const r = await sb.schema('morning_brief_v2').from('jobs').select('id').limit(1);
    out.select = r.error ? { ok: false, code: r.error.code, msg: r.error.message }
                         : { ok: true, count: r.data?.length };
  } catch (e) {
    out.select = { exception: String(e) };
  }

  // 2) INSERT (write)
  try {
    const r = await sb.schema('morning_brief_v2').from('jobs').insert({
      script: '__diag__',
      payload: { date: '1970-01-01' },
    }).select().single();
    out.insert = r.error ? { ok: false, code: r.error.code, msg: r.error.message, details: r.error.details }
                          : { ok: true, id: r.data?.id };
  } catch (e) {
    out.insert = { exception: String(e) };
  }

  // 3) Try insert via RPC call_next_job (which is in public schema and SECURITY DEFINER)
  // If this works, the table exists and we can write, just not via REST.
  try {
    const r = await sb.rpc('claim_next_job');
    out.claim_rpc = r.error ? { ok: false, code: r.error.code, msg: r.error.message }
                              : { ok: true, data: r.data };
  } catch (e) {
    out.claim_rpc = { exception: String(e) };
  }

  return res.status(200).json(out);
}
