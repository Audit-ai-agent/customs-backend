"""Add queryable chat provenance for atomic quota reservation."""

from pathlib import Path

from alembic import op

revision = "20260722_0007"
down_revision = "20260722_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "packages"
        / "operational_db"
        / "migrations"
        / "007_atomic_chat_quota.sql"
    )
    op.execute(path.read_text(encoding="utf-8"))


def downgrade() -> None:
    raise RuntimeError(
        "chat quota provenance is forward-only; roll back the application image "
        "and keep migration 007, then apply a reviewed corrective migration"
    )
