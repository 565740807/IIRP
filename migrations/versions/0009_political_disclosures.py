"""Retain government transaction disclosures and page checkpoints."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_political_disclosures"
down_revision = "0008_feed_group_issuer"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("political_person",
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('identity', sa.Text(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('district', sa.String(length=16), nullable=True),
        sa.Column('position', sa.Text(), nullable=True),
        sa.UniqueConstraint('source', 'identity', name='uq_political_person_identity'),
    )
    op.create_table("political_report",
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('external_id', sa.Text(), nullable=False),
        sa.Column('person_id', sa.String(length=36), sa.ForeignKey('political_person.id'), nullable=False),
        sa.Column('filed_date', sa.Date(), nullable=True),
        sa.Column('report_date', sa.Date(), nullable=True),
        sa.Column('report_date_source', sa.String(length=24), nullable=True),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('source_hash', sa.String(length=64), sa.ForeignKey('source_object.sha256'), nullable=True),
        sa.Column('amendment_of_id', sa.String(length=36), sa.ForeignKey('political_report.id'), nullable=True),
        sa.Column('amendment_status', sa.String(length=24), nullable=False),
        sa.Column('status', sa.String(length=24), nullable=False),
        sa.Column('parser_version', sa.String(length=40), nullable=True),
        sa.Column('page_count', sa.Integer(), nullable=False),
        sa.Column('pages_done', sa.Integer(), nullable=False),
        sa.Column('ocr_pages', sa.Integer(), nullable=False),
        sa.Column('ocr_seconds', sa.Numeric(precision=16, scale=3), nullable=False),
        sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('discovered_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('source', 'external_id', name='uq_political_report_external'),
    )
    op.create_index('ix_political_report_filed_date', 'political_report', ['filed_date'])
    op.create_index('ix_political_report_report_date', 'political_report', ['report_date'])
    op.create_index('ix_political_report_person_id', 'political_report', ['person_id'])
    op.create_table("political_page",
        sa.Column('report_id', sa.String(length=36), sa.ForeignKey('political_report.id'), nullable=False, primary_key=True),
        sa.Column('page', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('status', sa.String(length=24), nullable=False),
        sa.Column('ocr', sa.Boolean(), nullable=False),
        sa.Column('seconds', sa.Numeric(precision=16, scale=3), nullable=False),
        sa.Column('raw_text', sa.Text(), nullable=True),
        sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.create_table("political_transaction",
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('report_id', sa.String(length=36), sa.ForeignKey('political_report.id'), nullable=False),
        sa.Column('page', sa.Integer(), nullable=False),
        sa.Column('row_number', sa.Integer(), nullable=False),
        sa.Column('owner', sa.String(length=16), nullable=False),
        sa.Column('asset_name', sa.Text(), nullable=False),
        sa.Column('ticker', sa.String(length=24), nullable=True),
        sa.Column('ticker_origin', sa.String(length=16), nullable=True),
        sa.Column('asset_type', sa.String(length=24), nullable=False),
        sa.Column('option_type', sa.String(length=8), nullable=True),
        sa.Column('strike_price', sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column('expiration_date', sa.Date(), nullable=True),
        sa.Column('transaction_type', sa.String(length=24), nullable=False),
        sa.Column('transaction_date', sa.Date(), nullable=True),
        sa.Column('notification_date', sa.Date(), nullable=True),
        sa.Column('amount_low', sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column('amount_high', sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column('raw_row', sa.Text(), nullable=False),
        sa.Column('confidence', sa.String(length=16), nullable=False),
        sa.Column('warnings', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.UniqueConstraint('report_id', 'page', 'row_number', name='uq_political_transaction_row'),
    )
    op.create_index('ix_political_transaction_direction_date', 'political_transaction', ['transaction_type', 'transaction_date'])
    op.create_index('ix_political_transaction_report_id', 'political_transaction', ['report_id'])
    op.create_index('ix_political_transaction_ticker', 'political_transaction', ['ticker'])
    op.create_index('ix_political_transaction_transaction_date', 'political_transaction', ['transaction_date'])

def downgrade():
    op.drop_table('political_transaction')
    op.drop_table('political_page')
    op.drop_table('political_report')
    op.drop_table('political_person')
