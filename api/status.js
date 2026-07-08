// /root/morning_brief_v2/api/status.js
// GET /api/status?id=<job_id>&key=<shared-secret>
// Returns 200: { id, status, error, triggered_at, finished_at, script }

import { createClient } from '@supabase/supabase-js';

export default async function handler(req, res) {
  const { id, key } = req.query;
  if (!key || key !== process.env.PULT_SHARED_SECRET) {
    return res.status(401).json({ error: 'unauthorized' });
  }
  const jobId = parseInt(id, 10);
  if (!jobId || jobId <= 0) {
    return res.status(400).json({ error: 'invalid id' });
  }

  const supabaseUrl = process.env.SUPABASE_URL;
  const serviceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!supabaseUrl || !serviceKey) {
    return res.status(500).json({ error: 'server misconfigured' });
  }

  const sb = createClient(supabaseUrl, serviceKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  // Use SECURITY DEFINER RPC instead of direct SELECT (avoids .schema() routing bug)
  const { data, error } = await sb.rpc('get_job_status', { p_id: jobId });

  if (error) {
    return res.status(500).json({ error: 'rpc failed', details: error.message });
  }
  if (!data) {
    return res.status(404).json({ error: 'job not found' });
  }
  return res.status(200).json(data);
}

export const config = {
  maxDuration: 10,
};