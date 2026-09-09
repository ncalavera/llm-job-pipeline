CREATE TABLE IF NOT EXISTS vacancy_status_event (
    id INTEGER PRIMARY KEY,
    vacancy_id TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    status TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS vacancy_status_event_vacancy ON vacancy_status_event(vacancy_id, id);
CREATE TRIGGER IF NOT EXISTS vacancy_status_event_trigger AFTER UPDATE OF status ON vacancy
WHEN OLD.status IS NOT NEW.status
BEGIN
    INSERT INTO vacancy_status_event(vacancy_id, previous_status, status)
    VALUES (NEW.id, OLD.status, NEW.status);
END;
