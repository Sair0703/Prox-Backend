import { serve } from "https://deno.land/std@0.168.0/http/server.ts"
import { createClient } from "https://esm.sh/@supabase/supabase-js@2"

serve(async (req) => {
  const supabase = createClient(
    Deno.env.get('SUPABASE_URL')!,
    Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!
  )

  // 1. Initialize the built-in GTE-small model (384 dimensions)
  const model = new Supabase.ai.Session('gte-small');

  // 2. Fetch a batch of records missing embeddings
  const { data: records, error: fetchError } = await supabase
    .from('flyer_deals')
    .select('id, name, description')
    .is('embedding', null)
    .limit(50); // Small batches are safer for memory

  if (fetchError) return new Response(fetchError.message, { status: 500 });
  if (!records || records.length === 0) return new Response("All caught up!");

  // 3. Process the batch
  for (const record of records) {
    const text = `${record.name} ${record.description || ''}`;
    
    // Generate embedding using the local model
    const embedding = await model.run(text, { mean_pool: true, normalize: true });

    // Update the row
    await supabase
      .from('flyer_deals')
      .update({ embedding })
      .eq('id', record.id);
  }

  return new Response(`Processed ${records.length} records`, { status: 200 });
})