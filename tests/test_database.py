"""SQLite backup and restore."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.database import db
from src.database.repositories import Repositories


def _tickers(conn):
    return {row["ticker"] for row in conn.execute("SELECT ticker FROM companies").fetchall()}


def test_backup_and_restore_round_trip(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(db, "BACKUP_DIR", tmp_path / "backups")
    db_path = tmp_path / "live.db"

    conn = db.connect(db_path)
    db.init_db(conn)
    repos = Repositories(conn)
    repos.companies.upsert("AAA", name="First Company")
    conn.commit()

    backup = db.backup_database(conn, label="unit test")
    assert backup.exists()
    assert "unit" in backup.name

    # Change the live database after the backup was taken.
    repos.companies.upsert("BBB", name="Second Company")
    conn.execute("DELETE FROM companies WHERE ticker = 'AAA'")
    conn.commit()
    assert _tickers(conn) == {"BBB"}
    conn.close()

    restored = db.restore_database(backup, target=db_path)
    assert restored == db_path

    conn = db.connect(db_path)
    assert _tickers(conn) == {"AAA"}
    conn.close()

    # A safety copy of the pre-restore state is written before overwriting.
    safety_copies = list((tmp_path / "backups").glob("*pre-restore*.db"))
    assert safety_copies

    safety = db.connect(safety_copies[0], readonly=True)
    assert _tickers(safety) == {"BBB"}
    safety.close()


def test_restore_rejects_a_missing_backup(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        db.restore_database(tmp_path / "does-not-exist.db", target=tmp_path / "live.db")


def test_database_info_counts_stored_rows(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "live.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    conn = db.connect(db_path)
    db.init_db(conn)
    repos = Repositories(conn)
    repos.companies.upsert("AAA", name="First Company")
    conn.commit()
    info = db.database_info(conn)
    assert info["companies"] == 1
    assert info["schema_version"] == db.SCHEMA_VERSION
    assert info["path"] == str(db_path)
    conn.close()
