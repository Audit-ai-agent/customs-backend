"""Persist idempotent user interactions and rejected Agent responses."""

from pathlib import Path

from alembic import op

revision = "20260722_0006"
down_revision = "20260722_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "packages"
        / "operational_db"
        / "migrations"
        / "006_idempotent_interactions_and_response_state.sql"
    )
    op.execute(path.read_text(encoding="utf-8"))


def downgrade() -> None:
    raise RuntimeError("interaction lineage and call-state controls are forward-only")
