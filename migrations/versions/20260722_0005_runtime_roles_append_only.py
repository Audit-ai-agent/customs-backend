"""Separate runtime privilege and harden immutable ledgers."""

from pathlib import Path

from alembic import op

revision = "20260722_0005"
down_revision = "20260722_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "packages"
        / "operational_db"
        / "migrations"
        / "005_runtime_roles_append_only_and_logical_requests.sql"
    )
    op.execute(path.read_text(encoding="utf-8"))


def downgrade() -> None:
    raise RuntimeError("append-only security controls are forward-only; use a reviewed migration")
