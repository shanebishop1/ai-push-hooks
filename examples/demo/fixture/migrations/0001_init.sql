CREATE TABLE orders (
  id           TEXT PRIMARY KEY,
  customer_id  TEXT NOT NULL,
  total_cents  INTEGER NOT NULL,
  legacy_total TEXT,
  status       TEXT NOT NULL DEFAULT 'pending'
);
