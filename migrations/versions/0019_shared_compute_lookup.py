"""Index immutable cross-request result lookup; no payload rewrite."""
from alembic import op

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_analysis_result_shared_input', 'analysis_result',
                    ['security_id', 'input_key', 'created_at'])


def downgrade():
    op.drop_index('ix_analysis_result_shared_input', table_name='analysis_result')
