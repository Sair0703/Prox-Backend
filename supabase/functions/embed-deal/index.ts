import { serve } from "https://deno.land/std@0.168.0/http/server.ts"
import { createClient } from "https://esm.sh/@supabase/supabase-js@2"

const supabaseUrl = Deno.env.get('SUPABASE_URL')!
const supabaseKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!
const supabase = createClient(supabaseUrl, supabaseKey)

serve(async (req) => {
  const { record } = await req.json()

  // 1. Construct the text to embed
  // Combining name and description gives the vector more context
  const content = `Item: ${record.name}. Description: ${record.description}`

  // 2. Generate Embedding
  const session = new Supabase.ai.Session('gte-small');
  const embedding = await session.run(content, { mean_pool: true, normalize: true });

  // 3. Update the row with the new embedding
  const { error } = await supabase
    .from('flyer_deals')
    .update({ embedding })
    .eq('id', record.id)

  return new Response(JSON.stringify({ done: true }), { headers: { 'Content-Type': 'application/json' } })
})