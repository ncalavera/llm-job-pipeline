-- Timestamp of the loud "expiring" Telegram alert, so a protected role
-- (status='expiring') is alerted exactly once. Additive; IF NOT EXISTS keeps it
-- safe where the column was already added out of band.
ALTER TABLE vacancy ADD COLUMN IF NOT EXISTS expiring_alerted_at TIMESTAMPTZ;
