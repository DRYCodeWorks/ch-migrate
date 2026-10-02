"""r003: no-op, so the run shows a second version advance."""
from alembic import op

revision = "r003"
down_revision = "r002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SELECT 1")


def downgrade():
    pass
