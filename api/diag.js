// /root/morning_brief_v2/api/diag.js
// GET /api/diag?key=SHARED_SECRET
// Returns current grants on morning_brief_v2.jobs and lists all tables/schemas.
// Diagnostic only — remove after debugging.

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

  const out = { env_check: { SUPABASE_URL_len: supabaseUrl.length, SERVICE_ROLE_len: serviceKey.length } };

  // 1) Can service_role SELECT from jobs at all?
  try {
    const r = await sb.schema('morning_brief_v2').from('jobs').select('id').limit(1);
    out.sb_select = { error: r.error?.message, count: r.data?.length };
  } catch (e) {
    out.sb_select = { error: String(e) };
  }

  // 2) List all roles in DB
  try {
    const r = await sb.rpc('list_roles' as any).select();
    out.list_roles = r;
  } catch (e) {
    out.list_roles_error = String(e);
  }

  // 3) Get current_user (what role is service_role acting as)
  try {
    const r = await sb.rpc('get_current_role' as any).select();
    out.current_role = r;
  } catch (e) {
    out.current_role_error = String(e);
  }

  // 4) List grants on morning_brief_v2.jobs via RPC
  try {
    const r = await sb.rpc('list_table_grants' as any, { p_schema: 'morning_brief_v2', p_table: 'jobs' });
    out.grants = r;
  } catch (e) {
    out.grants_error = String(e);
  }

  // 5) Try raw SQL via rpc 'exec_sql' if available
  try {
    const r = await sb.rpc('exec_sql' as any, {
      q: "select grantee, privilege_type from information_schema.role_table_grants where table_schema='morning_brief_v2' and table_name='jobs' order by grantee, privilege_type"
    });
    out.role_table_grants = r;
  } catch (e) {
    out.role_table_grants_error = String(e);
  }

  return res.status(200).json(out);
}
