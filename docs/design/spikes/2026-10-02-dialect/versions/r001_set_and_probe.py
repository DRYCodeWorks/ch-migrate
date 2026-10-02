"""r001: standalone SET, then read it back in the same revision."""
import os

from alembic import op
from sqlalchemy import text

revision = "r001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    db = os.environ["CH_DATABASE"]
    op.execute(
        f"CREATE TABLE IF NOT EXISTS {db}.spike_probe "
        "(rev String, label String, val String, ts DateTime64(3) DEFAULT now64(3)) "
        "ENGINE = MergeTree ORDER BY ts"
    )
    op.execute("SET max_threads = 1")
    op.execute(
        f"INSERT INTO {db}.spike_probe (rev, label, val) "
        "SELECT 'r001', 'op.execute INSERT after SET', toString(getSetting('max_threads'))"
    )
    seen = op.get_bind().execute(text("SELECT getSetting('max_threads')")).scalar()
    print(f"SPIKE r001 get_bind().execute(SELECT getSetting('max_threads')) -> {seen!r}")


def downgrade():
    db = os.environ["CH_DATABASE"]
    op.execute(f"DROP TABLE IF EXISTS {db}.spike_probe")
