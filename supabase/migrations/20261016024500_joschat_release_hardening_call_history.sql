-- Applied to Supabase project fruldgczvyvumgmwmbqj on 2026-10-16.
-- Idempotent release hardening, no data deletion.
ALTER TABLE public.joschat ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.joschat FROM anon, authenticated;
CREATE INDEX IF NOT EXISTS idx_calls_caller_history ON public.calls (caller_id, started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_calls_callee_history ON public.calls (callee_id, started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_calls_conversation_history ON public.calls (conversation_id, started_at DESC, id DESC);
DROP POLICY IF EXISTS "Call participants can view their calls" ON public.calls;
CREATE POLICY "Call participants can view their calls" ON public.calls FOR SELECT TO authenticated
 USING ((caller_id = (SELECT auth.uid()) OR callee_id = (SELECT auth.uid()))
 AND EXISTS (SELECT 1 FROM public.conversation_participants cp
 WHERE cp.conversation_id = calls.conversation_id AND cp.user_id = (SELECT auth.uid())));
