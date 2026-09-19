ALTER TABLE orders ADD COLUMN refunded_cents INTEGER NOT NULL DEFAULT 0;

-- legacy_total is superseded by total_cents.
ALTER TABLE orders DROP COLUMN legacy_total;
