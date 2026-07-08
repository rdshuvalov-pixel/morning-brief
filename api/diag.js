// /root/morning_brief_v2/api/diag.js
// GET /api/diag?key=<shared-secret>
// Returns JSON with diagnostic info about Supabase access:
//   - env_check: lengths of SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY
//   - select:    result of .select() on jobs
//   - insert:    result of .insert() on jobs (cleanup test row afterwards)
//   - claim_rpc: result of rpc('claim_next_job')
//   - diag_rpc:  result of rpc('diag_current_role')

import { createClient } from '@supabase/supabase-js';

export default async function handler(req, res) {
  const { key } = req.query;
  if (!key || key !== process.env.PULT_SHARED_SECRET) {
    return res.status(401).json({ error: 'unauthorized' });
  }

  const supabaseUrl = process.env.SUPABASE_URL;
  const serviceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  const env_check = {
    SUPABASE_URL_len: supabaseUrl?.length || 0,
    SERVICE_ROLE_len: serviceKey?.length || 0,
  };

  if (!supabaseUrl || !serviceKey) {
    return res.status(500).json({ error: 'server misconfigured', env_check });
  }

  const sb = createClient(supabaseUrl, serviceKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  const out = { env_check, probes: {} };

  // Decode service_role JWT to verify it's really service_role (not anon or other)
  function decodeJwt(t) {
    try {
      const parts = t.split('.');
      if (parts.length !== 3) return { error: 'not a JWT' };
      let p = parts[1];
      // Manual base64url decode — Buffer.from can throw on negative count.
      p = p.replace(/-/g, '+').replace(/_/g, '/');
      while (p.length % 4) p += '=';
      return JSON.parse(Buffer.from(p, 'base64').toString('utf8'));
    } catch (e) { return { error: String(e) }; }
  }
  out.jwt_claims = decodeJwt(serviceKey);
  out.jwt_first_chars = serviceKey.slice(0, 50);
  out.jwt_last_chars = serviceKey.slice(-20);

  // Probe 1: SELECT
  try {
    const r = await sb.schema('morning_brief_v2').from('jobs').select('id, script, status').limit(1);
    out.probes.select = r.error
      ? { ok: false, code: r.error.code, msg: r.error.message }
      : { ok: true, count: r.data?.length, sample: r.data?.[0] };
  } catch (e) {
    out.probes.select = { ok: false, err: String(e) };
  }

  // Probe 2: INSERT (try, then DELETE test row)
  try {
    const r = await sb.schema('morning_brief_v2')
      .from('jobs')
      .insert({ script: '__diag__', status: 'pending', payload: { date: '1970-01-01', _diag: true } })
      .select()
      .single();
    if (r.error) {
      out.probes.insert = { ok: false, code: r.error.code, msg: r.error.message, details: r.error.details, hint: r.error.hint };
    } else {
      // Cleanup the test row
      const inserted_id = r.data?.id;
      const del = await sb.schema('morning_brief_v2').from('jobs').delete().eq('id', inserted_id);
      out.probes.insert = { ok: true, inserted_id, cleanup: del.error ? 'failed' : 'ok' };
    }
  } catch (e) {
    out.probes.insert = { ok: false, err: String(e) };
  }

  // Probe 3: claim_rpc
  try {
    const r = await sb.rpc('claim_next_job');
    out.probes.claim_rpc = r.error
      ? { ok: false, code: r.error.code, msg: r.error.message }
      : { ok: true, data: r.data };
  } catch (e) {
    out.probes.claim_rpc = { ok: false, err: String(e) };
  }

  // Probe 4: diag_rpc
  try {
    const r = await sb.rpc('diag_current_role');
    out.probes.diag_rpc = r.error
      ? { ok: false, code: r.error.code, msg: r.error.message }
      : { ok: true, data: r.data };
  } catch (e) {
    out.probes.diag_rpc = { ok: false, err: String(e) };
  }

  // Probe 5: list_recent_jobs (see actual job state)
  try {
    const r = await sb.rpc('list_recent_jobs', { p_limit: 10 });
    out.probes.list_recent = r.error
      ? { ok: false, code: r.error.code, msg: r.error.message }
      : { ok: true, count: r.data?.length, jobs: r.data };
  } catch (e) {
    out.probes.list_recent = { ok: false, err: String(e) };
  }

  return res.status(200).json(out);
}

export const config = {
  maxDuration: 10,
};