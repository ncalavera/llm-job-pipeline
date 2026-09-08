"""Changes are durable; repeated saves do not invent new events."""
import sqlite3
from pathlib import Path


def test_status_history_survives_deletion_and_ignores_unchanged_status():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE vacancy(id TEXT PRIMARY KEY, status TEXT)')
    sql = Path('sql/migrations/0030_vacancy_status_event.sqlite.sql').read_text()
    conn.executescript(sql)
    conn.executescript(sql)
    conn.execute("INSERT INTO vacancy VALUES ('v', 'applied')")
    conn.execute("UPDATE vacancy SET status='applied'")
    conn.execute("UPDATE vacancy SET status='declined'")
    conn.execute('DELETE FROM vacancy')
    assert conn.execute('SELECT vacancy_id,previous_status,status FROM vacancy_status_event').fetchall() == [('v','applied','declined')]
