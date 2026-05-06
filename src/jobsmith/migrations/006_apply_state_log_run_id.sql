-- run_id discriminator on apply_state_log (trk-60217f9f roborev job 954 HIGH).
--
-- The supervisor's transcript tailer must filter by something stronger
-- than slug because the orchestrator's ``rekey-slug`` step changes the
-- slug mid-run, and a slug-agnostic tail mixes concurrent runs and
-- replays historical rows from prior applications. ``run_id`` is the
-- per-supervisor-run discriminator already used by ``apply_runs``.
--
-- Schema change: nullable for back-compat. Pre-migration rows have
-- run_id IS NULL — readers MUST treat NULL as "any run / unknown" rather
-- than crashing. New writes always populate run_id.

ALTER TABLE apply_state_log ADD COLUMN run_id TEXT;

CREATE INDEX IF NOT EXISTS idx_apply_state_log_run_id
    ON apply_state_log(run_id, id);
