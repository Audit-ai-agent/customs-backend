"""Add durable Agent call-once and atomic decision constraints."""

from pathlib import Path

from alembic import op

revision = "20260722_0004"
down_revision = "20260721_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "packages"
        / "operational_db"
        / "migrations"
        / "004_agent_call_once_and_atomic_decisions.sql"
    )
    op.execute(path.read_text(encoding="utf-8"))


def downgrade() -> None:
    raise RuntimeError("call-once audit state is forward-only; use a reviewed migration")
