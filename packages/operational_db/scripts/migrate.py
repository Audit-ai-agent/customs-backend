from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"


def migrate(dsn: str) -> None:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("install operational_db dependencies before migrating") from exc

    files = sorted(MIGRATIONS.glob("*.sql"))
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE SCHEMA IF NOT EXISTS operations;
                CREATE TABLE IF NOT EXISTS operations.schema_migrations (
                  filename text PRIMARY KEY,
                  applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("SELECT filename FROM operations.schema_migrations")
            applied = {row[0] for row in cur.fetchall()}
        for path in files:
            if path.name in applied:
                print(f"skip  {path.name}")
                continue
            print(f"apply {path.name}")
            with conn.cursor() as cur:
                cur.execute(path.read_text(encoding="utf-8"))
                cur.execute(
                    "INSERT INTO operations.schema_migrations(filename) VALUES (%s)",
                    (path.name,),
                )
        conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dsn",
        default="postgresql://trade:change_me@localhost:55432/trade_doc",
    )
    args = parser.parse_args()
    migrate(args.dsn)


if __name__ == "__main__":
    main()
