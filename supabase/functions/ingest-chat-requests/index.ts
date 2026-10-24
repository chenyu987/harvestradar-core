// The collector Mac posts new requests (求单) here. It holds only LEADS_API_TOKEN, which
// can add rows to public.chat_requests and nothing else: no reads, no other tables.
// The table lives in the admin app's database (chenyu987/admin-app, migration 0029_chat_requests).
// Deploy with JWT verification off: the token below is the check.
//
// POST {"requests": [{dedup_key, group_name, sender, ...}]}
// header x-ingest-token: <LEADS_API_TOKEN>
// -> {"received": n, "inserted": m}   (rows already there are skipped by dedup_key)

import { createClient } from "jsr:@supabase/supabase-js@2";

const FIELDS = [
  "dedup_key", "group_name", "sender", "item_type", "location", "dates", "male", "female",
  "quote", "sent_at", "extractor_note", "captured_at", "screenshot", "model", "prompt_version",
] as const;
const MAX_ROWS = 200;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function sameToken(a: string, b: string) {
  if (!a || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);
  const token = Deno.env.get("LEADS_API_TOKEN") ?? "";
  if (!sameToken(req.headers.get("x-ingest-token") ?? "", token)) return json({ error: "bad token" }, 401);

  let body: { requests?: Record<string, unknown>[] };
  try {
    body = await req.json();
  } catch {
    return json({ error: "bad json" }, 400);
  }
  const requests = body.requests;
  if (!Array.isArray(requests) || requests.length > MAX_ROWS) return json({ error: `requests: array of at most ${MAX_ROWS}` }, 400);

  // Only known columns: status, person_id, handled_by etc. are for the admin app, not the collector.
  const rows = requests.map((l) => Object.fromEntries(FIELDS.filter((f) => f in l).map((f) => [f, l[f]])));
  if (rows.some((r) => !r.dedup_key || !r.group_name || !r.sender || !r.quote)) {
    return json({ error: "each request needs dedup_key, group_name, sender, quote" }, 400);
  }

  const db = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
  const { data, error } = await db.from("chat_requests")
    .upsert(rows, { onConflict: "dedup_key", ignoreDuplicates: true })
    .select("id");
  if (error) return json({ error: error.message }, 500);
  return json({ received: rows.length, inserted: data?.length ?? 0 });
});
