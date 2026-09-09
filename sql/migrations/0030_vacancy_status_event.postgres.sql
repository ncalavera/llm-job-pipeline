-- Preserve observed status changes from every writer. recorded_at is the time
-- the system learned a status, not an inferred interview/rejection date.
-- No cascading FK: removing a vacancy must not erase its history.
CREATE TABLE IF NOT EXISTS vacancy_status_event (
    id BIGSERIAL PRIMARY KEY,
    vacancy_id UUID NOT NULL,
    previous_status TEXT NOT NULL,
    status TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS vacancy_status_event_vacancy ON vacancy_status_event(vacancy_id, id);
CREATE OR REPLACE FUNCTION record_vacancy_status_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO vacancy_status_event(vacancy_id, previous_status, status)
    VALUES (NEW.id, OLD.status, NEW.status);
    RETURN NEW;
END $$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'vacancy_status_event_trigger' AND tgrelid = 'vacancy'::regclass) THEN
        CREATE TRIGGER vacancy_status_event_trigger AFTER UPDATE OF status ON vacancy
        FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
        EXECUTE FUNCTION record_vacancy_status_event();
    END IF;
END $$;
