// /root/morning_brief_v2/api/diag.js
// GET /api/diag?key=SHARED_SECRET
// Returns current grants on morning_brief_v2.jobs via service_role.

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

  // Try direct select to confirm service_role has access at all
  try {
    const r = await sb.schema('morning_brief_v2').from('jobs').select('id').limit(1);
    out.direct_select = { ok: !r.error, error: r.error?.message, rows: r.data?.length };
  } catch (e) {
    out.direct_select = { exception: String(e) };
  }

  // Try to read grants via information_schema
  try {
    // Need to use raw SQL — supabase-js doesn't expose it directly
    // Try rpc with a generic helper. If it doesn't exist, this will fail.
    const grants = await sb.rpc('get_table_grants', {
      schema_name: 'morning_brief_v2',
      table_name: 'jobs',
    });
    out.grants_rpc = grants;
  } catch (e) {
    out.grants_rpc_error = String(e);
  }

  return res.status(200).json(out);
}
