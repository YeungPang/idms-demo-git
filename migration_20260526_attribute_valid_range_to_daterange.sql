-- Safe phased migration from tsrange-based attribute.valid_range to daterange.
-- This script is idempotent and avoids destructive in-place type mutation.

BEGIN;

-- 1) Add a new generated daterange column for date semantics.
ALTER TABLE attribute
    ADD COLUMN IF NOT EXISTS valid_daterange daterange
    GENERATED ALWAYS AS (daterange(valid_from, valid_until, '[)')) STORED;

-- 2) Add GIST index for daterange queries.
CREATE INDEX IF NOT EXISTS idx_attributes_valid_daterange
    ON attribute USING GIST (valid_daterange);

-- 3) Keep legacy tsrange for compatibility during rollout.
-- Existing code can be switched gradually to valid_daterange.

COMMIT;

-- Optional cleanup (execute only after all callers are migrated):
-- DROP INDEX IF EXISTS idx_attributes_valid_range;
-- ALTER TABLE attribute DROP COLUMN IF EXISTS valid_range;
