"""Seed the local database with the synthetic demonstration dataset.

Usage::

    python scripts/seed_demo_data.py            # seed the default database
    python scripts/seed_demo_data.py --reset    # delete the database first
    python scripts/seed_demo_data.py --files-only

The demo companies do **not** exist. Their tickers are prefixed ``DEMO-`` and
every figure is generated arithmetically so the workflow can be exercised
without inventing data that could be mistaken for a real issuer.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import DB_PATH, SAMPLE_DIR, ensure_directories  # noqa: E402
from src.data.sample_data import seed_database, write_sample_files  # noqa: E402
from src.database.db import connect, init_db  # noqa: E402
from src.database.repositories import Repositories  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="delete the existing database file first")
    parser.add_argument("--files-only", action="store_true", help="write the demo CSVs without touching the database")
    parser.add_argument("--db", type=Path, default=None, help="database path (defaults to TERMINAL_DB_PATH)")
    args = parser.parse_args()

    ensure_directories()

    if args.files_only:
        paths = write_sample_files(SAMPLE_DIR)
        print("Wrote synthetic sample files (no issuers exist):")
        for label, path in paths.items():
            print(f"  {label:10s} {path}")
        return 0

    db_path = Path(args.db) if args.db else DB_PATH
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(db_path) + suffix)
            if candidate.exists():
                candidate.unlink()
                print(f"Removed {candidate}")

    conn = connect(db_path)
    init_db(conn)
    repos = Repositories(conn)
    repos.presets.seed_defaults()
    conn.commit()

    result = seed_database(repos)
    repos.imports.record(
        filename="synthetic-demo",
        tickers=result["companies"],
        rows=result["rows_imported"],
        errors=0,
        warnings=0,
        ok=True,
        detail=f"CLI seed; periods={result['periods_saved']}; prices={result['price_rows']}",
    )
    conn.commit()

    print(f"Database: {db_path}")
    print(f"  periods saved : {result['periods_saved']}")
    print(f"  rows imported : {result['rows_imported']:,}")
    print(f"  price rows    : {result['price_rows']:,}")
    print(f"  companies     : {', '.join(result['companies'])}")
    print(f"  validation    : {result['validation']}")
    print()
    print("All seeded companies are synthetic. Run `streamlit run app.py` to explore.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
