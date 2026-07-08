// /root/morning_brief_v2/api/status.js
// GET /api/status?id=<job_id>&key=<shared-secret>
// Returns 200: { id, status, error, triggered_at, finished_at, script }
// Returns 401 / 400 / 404 / 500.

import { createClient } from '@supabase/supabase-js';

export default async function handler(req, res) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'method not allowed' });
  }

  const { id, key } = req.query;
  if (!key || key !== process.env.PULT_SHARED_SECRET) {
    return res.status(401).json({ error: 'unauthorized' });
  }
  if (!id || !/^\d+$/.test(String(id))) {
    return res.status(400).json({ error: 'id must be a positive integer' });
  }

  const supabaseUrl = process.env.SUPABASE_URL;
  const serviceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!supabaseUrl || !serviceKey) {
    return res.status(500).json({ error: 'server misconfigured' });
  }

  const sb = createClient(supabaseUrl, serviceKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  const { data, error } = await sb
    .schema('morning_brief_v2')
    .from('jobs')
    .select('id, status, error, triggered_at, finished_at, script, payload')
    .eq('id', id)
    .single();

  if (error) {
    if (error.code === 'PGRST116') {
      return res.status(404).json({ error: 'not found' });
    }
    return res.status(500).json({ error: 'select failed', details: error.message });
  }

  return res.status(200).json(data);
}

export const config = {
  maxDuration: 10,
};
