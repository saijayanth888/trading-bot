-- 003_run_state.sql — singleton run-state row.
--
-- Post-cutover, the dashboard's Pause/Resume buttons + the kill switch
-- need to signal the V4 runner. Pre-cutover this went over freqtrade's
-- /api/v1/{stop,start}; freqtrade is dead. The replacement is a single
-- row in quanta_schema.run_state that:
--   1. /api/ops/pause UPSERTs paused=true + reason
--   2. /api/ops/resume UPSERTs paused=false
--   3. run_v4_shadow.py reads at the top of every cycle and short-circuits
--      proposal/order generation when paused
--
-- One row enforced via CHECK constraint (id=1).

CREATE TABLE IF NOT EXISTS run_state (
    id            SMALLINT PRIMARY KEY CHECK (id = 1),
    paused        BOOLEAN  NOT NULL DEFAULT FALSE,
    paused_reason TEXT,
    paused_at     TIMESTAMPTZ,
    set_by        TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO run_state (id, paused, set_by)
VALUES (1, FALSE, 'migration')
ON CONFLICT (id) DO NOTHING;

INSERT INTO quanta_schema_version (version, description)
VALUES (3, 'singleton run_state (pause/resume kill switch)')
ON CONFLICT (version) DO NOTHING;
