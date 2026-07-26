"""Add product workbench persistence without modifying legacy snapshots."""

from pathlib import Path

from alembic import op

revision = "20260721_0003"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "packages"
        / "operational_db"
        / "migrations"
        / "003_product_workbench.sql"
    )
    op.execute(path.read_text(encoding="utf-8"))


def downgrade() -> None:
    # Product audit and immutable run history must not be destructively removed.
    raise RuntimeError("workbench migration is forward-only; restore via a reviewed migration")
