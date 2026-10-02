"""r002: no SET; does r001's SET survive into the next revision?"""
import os

from alembic import op
from sqlalchemy import text

revision = "r002"
down_revision = "r001"
branch_labels = None
depends_on = None


def upgrade():
    db = os.environ["CH_DATABASE"]
    op.execute(
        f"INSERT INTO {db}.spike_probe (rev, label, val) "
        "SELECT 'r002', 'next revision, no SET', toString(getSetting('max_threads'))"
    )
    seen = op.get_bind().execute(text("SELECT getSetting('max_threads')")).scalar()
    print(f"SPIKE r002 get_bind().execute(SELECT getSetting('max_threads')) -> {seen!r}")


def downgrade():
    pass
