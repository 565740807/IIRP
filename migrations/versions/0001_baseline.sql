-- IIRP database schema baseline (PostgreSQL 18).
-- Produced with pg_dump --schema-only from the schema the earlier migrations
-- 0001-0031 built (they remain in the git history), and checked to be identical
-- to an existing installation's schema. Later changes go in new migrations.

CREATE TABLE public.amendment_relation (
    id character varying(36) NOT NULL,
    accession character varying(20) NOT NULL,
    original_event_id character varying(36),
    amended_event_id character varying(36),
    action character varying(24) NOT NULL,
    evidence jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL
);

CREATE TABLE public.analysis_request (
    id character varying(36) NOT NULL,
    batch_id character varying(36) NOT NULL,
    params jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL
);

CREATE TABLE public.analysis_result (
    id character varying(36) NOT NULL,
    analysis_id character varying(36) NOT NULL,
    security_id character varying(36) NOT NULL,
    input_key character varying(64) NOT NULL,
    inputs jsonb NOT NULL,
    data jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL,
    accessed_at timestamp with time zone NOT NULL,
    payload bytea,
    overlap_projection jsonb,
    expires_at timestamp with time zone,
    CONSTRAINT ck_analysis_result_projection_pair CHECK ((((payload IS NULL) AND (overlap_projection IS NULL)) OR ((payload IS NOT NULL) AND (overlap_projection IS NOT NULL) AND (data = '{}'::jsonb))))
);
ALTER TABLE ONLY public.analysis_result ALTER COLUMN data SET COMPRESSION lz4;
ALTER TABLE ONLY public.analysis_result ALTER COLUMN payload SET STORAGE EXTERNAL;
ALTER TABLE ONLY public.analysis_result ALTER COLUMN overlap_projection SET COMPRESSION lz4;

CREATE TABLE public.batch (
    id character varying(36) NOT NULL,
    request_id character varying(128) NOT NULL,
    scope_key character varying(64) NOT NULL,
    kind character varying(32) NOT NULL,
    title text NOT NULL,
    params jsonb NOT NULL,
    trigger character varying(16) NOT NULL,
    policy_key character varying(32),
    status character varying(24) NOT NULL,
    requested_action character varying(16),
    control_version integer NOT NULL,
    parent_id character varying(36),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    last_planned_at timestamp with time zone,
    planning_failures integer DEFAULT 0 NOT NULL,
    planning_error jsonb,
    planning_retry_at timestamp with time zone
);

CREATE TABLE public.batch_job (
    scope_id character varying(36) NOT NULL,
    job_id character varying(36) NOT NULL,
    active boolean NOT NULL
);

CREATE TABLE public.batch_plan_signal (
    batch_id character varying(36) NOT NULL,
    token character varying(36) NOT NULL,
    updated_at timestamp with time zone NOT NULL
);

CREATE TABLE public.collection_policy (
    id integer NOT NULL,
    sec_enabled boolean NOT NULL,
    version integer NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    next_run_at timestamp with time zone
);

CREATE SEQUENCE public.collection_policy_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.collection_policy_id_seq OWNED BY public.collection_policy.id;

CREATE TABLE public.collection_strategy (
    key character varying(32) NOT NULL,
    enabled boolean NOT NULL,
    version integer NOT NULL,
    options jsonb NOT NULL,
    next_run_at timestamp with time zone,
    last_run_at timestamp with time zone
);

CREATE TABLE public.coverage (
    provider character varying(32) NOT NULL,
    target character varying(64) NOT NULL,
    status character varying(32) NOT NULL,
    message text NOT NULL,
    source_hash character varying(64),
    updated_at timestamp with time zone NOT NULL
);

CREATE TABLE public.coverage_segment (
    id character varying(36) NOT NULL,
    security_id character varying(36),
    provider character varying(32) NOT NULL,
    kind character varying(32) NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    status character varying(32) NOT NULL,
    details jsonb NOT NULL,
    source_hash character varying(64),
    checked_at timestamp with time zone NOT NULL
);

CREATE TABLE public.event_set (
    id character varying(36) NOT NULL,
    title text NOT NULL,
    kind character varying(16) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    events jsonb NOT NULL,
    request_id character varying(128)
);

CREATE TABLE public.export_manifest (
    id character varying(36) NOT NULL,
    result_ids jsonb NOT NULL,
    params jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL
);

CREATE TABLE public.feed_group_current (
    group_key character varying(32) NOT NULL,
    revision_id character varying(36) NOT NULL,
    issuer_id character varying(10) NOT NULL,
    accepted_at timestamp with time zone NOT NULL,
    row_count integer NOT NULL,
    seq bigint,
    xid xid8
);

CREATE TABLE public.feed_group_order (
    kind character varying(16) NOT NULL,
    sort_order character varying(16) NOT NULL,
    group_key character varying(32) NOT NULL COLLATE pg_catalog."C",
    sort_key timestamp with time zone NOT NULL,
    accepted_at timestamp with time zone NOT NULL,
    revision_id character varying(36) NOT NULL,
    seq bigint,
    xid xid8
);

CREATE TABLE public.feed_group_revision (
    id character varying(36) NOT NULL,
    group_key character varying(32) NOT NULL,
    issuer_id character varying(10) NOT NULL,
    accepted_at timestamp with time zone NOT NULL,
    data jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL,
    match_kinds jsonb NOT NULL,
    row_count integer NOT NULL,
    transaction_sort_dates jsonb DEFAULT '{}'::jsonb NOT NULL,
    seq bigint,
    xid xid8 DEFAULT pg_current_xact_id()
);

CREATE SEQUENCE public.feed_revision_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.feed_revision_seq OWNED BY public.feed_group_revision.seq;

CREATE TABLE public.feed_session (
    id character varying(36) NOT NULL,
    revision_ids jsonb NOT NULL,
    filters jsonb NOT NULL,
    expires_at timestamp with time zone NOT NULL
);

CREATE TABLE public.feed_watermark_cluster (
    id integer DEFAULT 1 NOT NULL,
    system_identifier text NOT NULL,
    CONSTRAINT feed_watermark_cluster_id_check CHECK ((id = 1))
);

CREATE TABLE public.filing (
    accession character varying(20) NOT NULL,
    form character varying(8) NOT NULL,
    filing_date date,
    accepted_at timestamp with time zone,
    first_seen_at timestamp with time zone NOT NULL,
    issuer_id character varying(10),
    index_url text NOT NULL,
    status character varying(32) NOT NULL,
    current_version character varying(36),
    visible boolean NOT NULL
);

CREATE TABLE public.filing_owner (
    version_id character varying(36) NOT NULL,
    owner_id character varying(10) NOT NULL,
    relationship jsonb NOT NULL
);

CREATE TABLE public.filing_version (
    id character varying(36) NOT NULL,
    accession character varying(20) NOT NULL,
    source_hash character varying(64) NOT NULL,
    parser_version character varying(32) NOT NULL,
    data jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL
);

CREATE TABLE public.issuer (
    id character varying(10) NOT NULL,
    name text NOT NULL,
    ticker character varying(32)
);

CREATE TABLE public.job (
    id character varying(36) NOT NULL,
    kind character varying(32) NOT NULL,
    title text NOT NULL,
    target jsonb NOT NULL,
    idempotency_key character varying(64) NOT NULL,
    status character varying(24) NOT NULL,
    trigger character varying(16) NOT NULL,
    priority integer NOT NULL,
    requested_action character varying(16),
    control_version integer NOT NULL,
    progress_done integer NOT NULL,
    progress_total integer NOT NULL,
    checkpoint jsonb NOT NULL,
    result jsonb,
    error text,
    attempts integer NOT NULL,
    available_at timestamp with time zone NOT NULL,
    lease_token character varying(36),
    lease_until timestamp with time zone,
    heartbeat_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    CONSTRAINT ck_job_status CHECK (status IN ('QUEUED','RUNNING','SUCCEEDED','PARTIAL','FAILED','PAUSE_REQUESTED','PAUSED','CANCEL_REQUESTED','CANCELLED','RETRY_WAIT')),
    CONSTRAINT ck_progress CHECK (((progress_done >= 0) AND (progress_done <= progress_total)))
);

CREATE TABLE public.job_dependency (
    job_id character varying(36) NOT NULL,
    prerequisite_id character varying(36) NOT NULL
);

CREATE TABLE public.job_subscription (
    job_id character varying(36) NOT NULL,
    source character varying(16) NOT NULL,
    active boolean NOT NULL
);

CREATE TABLE public.maintenance_run (
    id character varying(36) NOT NULL,
    kind character varying(32) NOT NULL,
    status character varying(24) NOT NULL,
    details jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL
);

CREATE TABLE public.market_quote_cache (
    symbol character varying(32) NOT NULL,
    data jsonb NOT NULL,
    fetched_at timestamp with time zone NOT NULL
);

CREATE TABLE public.price_cache (
    id character varying(36) NOT NULL,
    security_id character varying(36) NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    complete_through date NOT NULL,
    provider character varying(32) NOT NULL,
    details jsonb NOT NULL,
    fetched_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL
);

CREATE TABLE public.price_cache_bar (
    cache_id character varying(36) NOT NULL,
    session_date date NOT NULL,
    open numeric(30,12),
    high numeric(30,12),
    low numeric(30,12),
    close numeric(30,12),
    adj_close numeric(30,12),
    volume numeric(32,4),
    dividends numeric(30,12),
    splits numeric(30,12),
    status character varying(32) NOT NULL,
    reason text
);

CREATE TABLE public.prompt_template (
    kind character varying(16) NOT NULL,
    language character varying(8) NOT NULL,
    text text NOT NULL,
    updated_at timestamp with time zone NOT NULL
);

CREATE TABLE public.reporting_owner (
    id character varying(10) NOT NULL,
    name text NOT NULL
);

CREATE TABLE public.request_receipt (
    request_id character varying(128) NOT NULL,
    batch_id character varying(36) NOT NULL,
    scope_key character varying(64) NOT NULL
);

CREATE TABLE public.request_scope (
    id character varying(36) NOT NULL,
    batch_id character varying(36) NOT NULL,
    symbol character varying(32) NOT NULL,
    security_id character varying(36),
    start_date date,
    end_date date,
    status character varying(32) NOT NULL,
    wait_reason text,
    checkpoint jsonb NOT NULL
);

CREATE TABLE public.research_track (
    origin_id character varying(36) NOT NULL,
    latest_id character varying(36) NOT NULL,
    fingerprint character varying(64),
    checked_at timestamp with time zone NOT NULL
);

CREATE TABLE public.security (
    id character varying(36) NOT NULL,
    symbol character varying(32) NOT NULL,
    name text NOT NULL,
    issuer_id character varying(10),
    instrument character varying(32) NOT NULL,
    currency character varying(16),
    exchange character varying(32),
    calendar character varying(32),
    status character varying(32) NOT NULL,
    metadata_json jsonb NOT NULL
);

CREATE TABLE public.security_identifier (
    id character varying(36) NOT NULL,
    security_id character varying(36) NOT NULL,
    provider character varying(32) NOT NULL,
    symbol character varying(32) NOT NULL,
    valid_from date NOT NULL,
    valid_to date
);

CREATE TABLE public.source_budget (
    provider character varying(32) NOT NULL,
    next_allowed_at timestamp with time zone NOT NULL,
    failures integer NOT NULL
);

CREATE TABLE public.source_object (
    sha256 character varying(64) NOT NULL,
    relative_path text NOT NULL,
    byte_size integer NOT NULL,
    media_type character varying(100) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone
);

CREATE TABLE public.source_observation (
    id character varying(36) NOT NULL,
    job_id character varying(36),
    source_hash character varying(64) NOT NULL,
    provider character varying(32) NOT NULL,
    params jsonb NOT NULL,
    observed_at timestamp with time zone NOT NULL
);

CREATE TABLE public.source_poll (
    source character varying(32) NOT NULL,
    watermark_at timestamp with time zone,
    catchup jsonb,
    next_poll_at timestamp with time zone NOT NULL,
    last_polled_at timestamp with time zone,
    last_success_at timestamp with time zone,
    last_complete_at timestamp with time zone,
    failures integer DEFAULT 0 NOT NULL,
    last_error text,
    last_result jsonb,
    lease_token character varying(36),
    lease_until timestamp with time zone,
    updated_at timestamp with time zone NOT NULL
);

CREATE TABLE public.transaction_event (
    id character varying(36) NOT NULL,
    issuer_id character varying(10) NOT NULL,
    accession character varying(20) NOT NULL,
    version_id character varying(36) NOT NULL,
    row_key character varying(100) NOT NULL,
    transaction_date date,
    accepted_at timestamp with time zone,
    data jsonb NOT NULL,
    owner_ids jsonb NOT NULL,
    status character varying(32) NOT NULL,
    replaces_id character varying(36)
);

CREATE TABLE public.user_preferences (
    id integer NOT NULL,
    "values" jsonb NOT NULL,
    version integer NOT NULL
);

CREATE SEQUENCE public.user_preferences_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.user_preferences_id_seq OWNED BY public.user_preferences.id;

CREATE TABLE public.worker_heartbeat (
    id character varying(48) NOT NULL,
    last_seen timestamp with time zone NOT NULL
);

ALTER TABLE ONLY public.collection_policy ALTER COLUMN id SET DEFAULT nextval('public.collection_policy_id_seq'::regclass);

ALTER TABLE ONLY public.feed_group_revision ALTER COLUMN seq SET DEFAULT nextval('public.feed_revision_seq'::regclass);

ALTER TABLE ONLY public.user_preferences ALTER COLUMN id SET DEFAULT nextval('public.user_preferences_id_seq'::regclass);

ALTER TABLE ONLY public.amendment_relation
    ADD CONSTRAINT amendment_relation_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.analysis_request
    ADD CONSTRAINT analysis_request_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.analysis_result
    ADD CONSTRAINT analysis_result_analysis_id_security_id_input_key_key UNIQUE (analysis_id, security_id, input_key);

ALTER TABLE ONLY public.analysis_result
    ADD CONSTRAINT analysis_result_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.batch_job
    ADD CONSTRAINT batch_job_pkey PRIMARY KEY (scope_id, job_id);

ALTER TABLE ONLY public.batch
    ADD CONSTRAINT batch_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.batch_plan_signal
    ADD CONSTRAINT batch_plan_signal_pkey PRIMARY KEY (batch_id);

ALTER TABLE ONLY public.batch
    ADD CONSTRAINT batch_request_id_key UNIQUE (request_id);

ALTER TABLE ONLY public.collection_policy
    ADD CONSTRAINT collection_policy_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.collection_strategy
    ADD CONSTRAINT collection_strategy_pkey PRIMARY KEY (key);

ALTER TABLE ONLY public.coverage
    ADD CONSTRAINT coverage_pkey PRIMARY KEY (provider, target);

ALTER TABLE ONLY public.coverage_segment
    ADD CONSTRAINT coverage_segment_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.event_set
    ADD CONSTRAINT event_set_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.event_set
    ADD CONSTRAINT event_set_request_id_key UNIQUE (request_id);

ALTER TABLE ONLY public.export_manifest
    ADD CONSTRAINT export_manifest_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.feed_group_current
    ADD CONSTRAINT feed_group_current_pkey PRIMARY KEY (group_key);

ALTER TABLE ONLY public.feed_group_order
    ADD CONSTRAINT feed_group_order_pkey PRIMARY KEY (kind, sort_order, group_key);

ALTER TABLE ONLY public.feed_group_revision
    ADD CONSTRAINT feed_group_revision_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.feed_session
    ADD CONSTRAINT feed_session_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.feed_watermark_cluster
    ADD CONSTRAINT feed_watermark_cluster_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.filing_owner
    ADD CONSTRAINT filing_owner_pkey PRIMARY KEY (version_id, owner_id);

ALTER TABLE ONLY public.filing
    ADD CONSTRAINT filing_pkey PRIMARY KEY (accession);

ALTER TABLE ONLY public.filing_version
    ADD CONSTRAINT filing_version_accession_source_hash_parser_version_key UNIQUE (accession, source_hash, parser_version);

ALTER TABLE ONLY public.filing_version
    ADD CONSTRAINT filing_version_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.issuer
    ADD CONSTRAINT issuer_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.job_dependency
    ADD CONSTRAINT job_dependency_pkey PRIMARY KEY (job_id, prerequisite_id);

ALTER TABLE ONLY public.job
    ADD CONSTRAINT job_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.job_subscription
    ADD CONSTRAINT job_subscription_pkey PRIMARY KEY (job_id, source);

ALTER TABLE ONLY public.maintenance_run
    ADD CONSTRAINT maintenance_run_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.market_quote_cache
    ADD CONSTRAINT market_quote_cache_pkey PRIMARY KEY (symbol);

ALTER TABLE ONLY public.price_cache_bar
    ADD CONSTRAINT price_cache_bar_pkey PRIMARY KEY (cache_id, session_date);

ALTER TABLE ONLY public.price_cache
    ADD CONSTRAINT price_cache_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.price_cache
    ADD CONSTRAINT price_cache_security_id_key UNIQUE (security_id);

ALTER TABLE ONLY public.prompt_template
    ADD CONSTRAINT prompt_template_pkey PRIMARY KEY (kind, language);

ALTER TABLE ONLY public.reporting_owner
    ADD CONSTRAINT reporting_owner_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.request_receipt
    ADD CONSTRAINT request_receipt_pkey PRIMARY KEY (request_id);

ALTER TABLE ONLY public.request_scope
    ADD CONSTRAINT request_scope_batch_id_symbol_key UNIQUE (batch_id, symbol);

ALTER TABLE ONLY public.request_scope
    ADD CONSTRAINT request_scope_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.research_track
    ADD CONSTRAINT research_track_pkey PRIMARY KEY (origin_id);

ALTER TABLE ONLY public.security_identifier
    ADD CONSTRAINT security_identifier_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.security_identifier
    ADD CONSTRAINT security_identifier_provider_symbol_valid_from_key UNIQUE (provider, symbol, valid_from);

ALTER TABLE ONLY public.security
    ADD CONSTRAINT security_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.source_budget
    ADD CONSTRAINT source_budget_pkey PRIMARY KEY (provider);

ALTER TABLE ONLY public.source_object
    ADD CONSTRAINT source_object_pkey PRIMARY KEY (sha256);

ALTER TABLE ONLY public.source_object
    ADD CONSTRAINT source_object_relative_path_key UNIQUE (relative_path);

ALTER TABLE ONLY public.source_observation
    ADD CONSTRAINT source_observation_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.source_poll
    ADD CONSTRAINT source_poll_pkey PRIMARY KEY (source);

ALTER TABLE ONLY public.transaction_event
    ADD CONSTRAINT transaction_event_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.transaction_event
    ADD CONSTRAINT transaction_event_version_id_row_key_key UNIQUE (version_id, row_key);

ALTER TABLE ONLY public.user_preferences
    ADD CONSTRAINT user_preferences_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.worker_heartbeat
    ADD CONSTRAINT worker_heartbeat_pkey PRIMARY KEY (id);

CREATE INDEX ix_analysis_request_batch_id ON public.analysis_request USING btree (batch_id);

CREATE INDEX ix_analysis_result_analysis_id ON public.analysis_result USING btree (analysis_id);

CREATE INDEX ix_analysis_result_expires_at ON public.analysis_result USING btree (expires_at);

CREATE INDEX ix_analysis_result_shared_input ON public.analysis_result USING btree (security_id, input_key, created_at);

CREATE INDEX ix_batch_job_job_active ON public.batch_job USING btree (job_id, active);

CREATE INDEX ix_batch_last_planned_at ON public.batch USING btree (last_planned_at);

CREATE INDEX ix_batch_plan_signal_updated_at ON public.batch_plan_signal USING btree (updated_at);

CREATE INDEX ix_batch_scope_key ON public.batch USING btree (scope_key);

CREATE INDEX ix_batch_status ON public.batch USING btree (status);

CREATE INDEX ix_coverage_segment_security_id ON public.coverage_segment USING btree (security_id);

CREATE INDEX ix_event_issuer_date ON public.transaction_event USING btree (issuer_id, transaction_date, id);

CREATE INDEX ix_event_recent_accepted_at ON public.transaction_event USING btree (accepted_at DESC NULLS LAST, id DESC);

CREATE INDEX ix_event_recent_transaction_date ON public.transaction_event USING btree (transaction_date DESC NULLS LAST, id DESC);

CREATE INDEX ix_feed_group_current_seq ON public.feed_group_current USING btree (seq) WHERE (seq IS NOT NULL);

CREATE INDEX ix_feed_group_current_xid ON public.feed_group_current USING btree (xid) WHERE (xid IS NOT NULL);

CREATE INDEX ix_feed_group_order_page ON public.feed_group_order USING btree (kind, sort_order, sort_key DESC, accepted_at DESC, group_key DESC);

CREATE INDEX ix_feed_group_revision_accepted_at ON public.feed_group_revision USING btree (accepted_at);

CREATE INDEX ix_feed_group_revision_group_key ON public.feed_group_revision USING btree (group_key);

CREATE INDEX ix_feed_group_revision_seq ON public.feed_group_revision USING btree (seq) WHERE (seq IS NOT NULL);

CREATE INDEX ix_feed_group_revision_xid ON public.feed_group_revision USING btree (xid) WHERE (xid IS NOT NULL);

CREATE INDEX ix_filing_accepted_at ON public.filing USING btree (accepted_at);

CREATE INDEX ix_filing_issuer_id ON public.filing USING btree (issuer_id);

CREATE INDEX ix_filing_pending ON public.filing USING btree (accession) WHERE ((visible IS TRUE) AND (current_version IS NULL));

CREATE INDEX ix_filing_version_accession ON public.filing_version USING btree (accession);

CREATE INDEX ix_filing_version_source_hash ON public.filing_version USING btree (source_hash);

CREATE INDEX ix_issuer_ticker ON public.issuer USING btree (ticker);

CREATE INDEX ix_job_claim ON public.job USING btree (status, available_at, priority);

CREATE INDEX ix_job_created_id ON public.job USING btree (created_at DESC, id DESC);

CREATE INDEX ix_job_history_key ON public.job USING btree (idempotency_key, created_at DESC);

CREATE INDEX ix_job_kind_status_finished ON public.job USING btree (kind, status, finished_at);

CREATE INDEX ix_job_sec_claim_stamp ON public.job USING btree (kind, ((checkpoint ->> '_queue_sec_claimed_at'::text)) DESC) WHERE ((checkpoint ->> '_queue_sec_claimed_at'::text) IS NOT NULL);

CREATE INDEX ix_job_sec_document_accession_latest ON public.job USING btree (((target ->> 'accession'::text)), created_at DESC, id DESC) WHERE ((kind)::text = 'sec_document'::text);

CREATE INDEX ix_price_cache_expires_at ON public.price_cache USING btree (expires_at);

CREATE INDEX ix_request_scope_batch_id ON public.request_scope USING btree (batch_id);

CREATE INDEX ix_request_scope_security_id ON public.request_scope USING btree (security_id);

CREATE INDEX ix_research_track_latest_id ON public.research_track USING btree (latest_id);

CREATE INDEX ix_security_identifier_security_id ON public.security_identifier USING btree (security_id);

CREATE INDEX ix_security_identifier_symbol ON public.security_identifier USING btree (symbol);

CREATE INDEX ix_security_issuer_id ON public.security USING btree (issuer_id);

CREATE INDEX ix_security_symbol ON public.security USING btree (symbol);

CREATE INDEX ix_source_object_expires_at ON public.source_object USING btree (expires_at) WHERE (expires_at IS NOT NULL);

CREATE INDEX ix_source_observation_job_id ON public.source_observation USING btree (job_id);

CREATE INDEX ix_source_observation_source_hash ON public.source_observation USING btree (source_hash);

CREATE INDEX ix_transaction_event_accepted_at ON public.transaction_event USING btree (accepted_at);

CREATE INDEX ix_transaction_event_accession ON public.transaction_event USING btree (accession);

CREATE INDEX ix_transaction_event_issuer_id ON public.transaction_event USING btree (issuer_id);

CREATE INDEX ix_transaction_event_transaction_date ON public.transaction_event USING btree (transaction_date);

CREATE UNIQUE INDEX uq_active_job_key ON public.job USING btree (idempotency_key) WHERE status IN ('QUEUED','RUNNING','PAUSE_REQUESTED','PAUSED','CANCEL_REQUESTED','RETRY_WAIT');

ALTER TABLE ONLY public.amendment_relation
    ADD CONSTRAINT amendment_relation_accession_fkey FOREIGN KEY (accession) REFERENCES public.filing(accession);

ALTER TABLE ONLY public.amendment_relation
    ADD CONSTRAINT amendment_relation_amended_event_id_fkey FOREIGN KEY (amended_event_id) REFERENCES public.transaction_event(id);

ALTER TABLE ONLY public.amendment_relation
    ADD CONSTRAINT amendment_relation_original_event_id_fkey FOREIGN KEY (original_event_id) REFERENCES public.transaction_event(id);

ALTER TABLE ONLY public.analysis_request
    ADD CONSTRAINT analysis_request_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.batch(id);

ALTER TABLE ONLY public.analysis_result
    ADD CONSTRAINT analysis_result_analysis_id_fkey FOREIGN KEY (analysis_id) REFERENCES public.analysis_request(id);

ALTER TABLE ONLY public.analysis_result
    ADD CONSTRAINT analysis_result_security_id_fkey FOREIGN KEY (security_id) REFERENCES public.security(id);

ALTER TABLE ONLY public.batch_job
    ADD CONSTRAINT batch_job_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.job(id);

ALTER TABLE ONLY public.batch_job
    ADD CONSTRAINT batch_job_scope_id_fkey FOREIGN KEY (scope_id) REFERENCES public.request_scope(id);

ALTER TABLE ONLY public.batch
    ADD CONSTRAINT batch_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.batch(id);

ALTER TABLE ONLY public.batch_plan_signal
    ADD CONSTRAINT batch_plan_signal_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.batch(id);

ALTER TABLE ONLY public.coverage_segment
    ADD CONSTRAINT coverage_segment_security_id_fkey FOREIGN KEY (security_id) REFERENCES public.security(id);

ALTER TABLE ONLY public.coverage_segment
    ADD CONSTRAINT coverage_segment_source_hash_fkey FOREIGN KEY (source_hash) REFERENCES public.source_object(sha256);

ALTER TABLE ONLY public.coverage
    ADD CONSTRAINT coverage_source_hash_fkey FOREIGN KEY (source_hash) REFERENCES public.source_object(sha256);

ALTER TABLE ONLY public.feed_group_current
    ADD CONSTRAINT feed_group_current_revision_id_fkey FOREIGN KEY (revision_id) REFERENCES public.feed_group_revision(id);

ALTER TABLE ONLY public.feed_group_revision
    ADD CONSTRAINT feed_group_revision_issuer_id_fkey FOREIGN KEY (issuer_id) REFERENCES public.issuer(id);

ALTER TABLE ONLY public.filing
    ADD CONSTRAINT filing_issuer_id_fkey FOREIGN KEY (issuer_id) REFERENCES public.issuer(id);

ALTER TABLE ONLY public.filing_owner
    ADD CONSTRAINT filing_owner_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.reporting_owner(id);

ALTER TABLE ONLY public.filing_owner
    ADD CONSTRAINT filing_owner_version_id_fkey FOREIGN KEY (version_id) REFERENCES public.filing_version(id);

ALTER TABLE ONLY public.filing_version
    ADD CONSTRAINT filing_version_accession_fkey FOREIGN KEY (accession) REFERENCES public.filing(accession);

ALTER TABLE ONLY public.filing_version
    ADD CONSTRAINT filing_version_source_hash_fkey FOREIGN KEY (source_hash) REFERENCES public.source_object(sha256);

ALTER TABLE ONLY public.job_dependency
    ADD CONSTRAINT job_dependency_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.job(id);

ALTER TABLE ONLY public.job_dependency
    ADD CONSTRAINT job_dependency_prerequisite_id_fkey FOREIGN KEY (prerequisite_id) REFERENCES public.job(id);

ALTER TABLE ONLY public.job_subscription
    ADD CONSTRAINT job_subscription_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.job(id);

ALTER TABLE ONLY public.price_cache_bar
    ADD CONSTRAINT price_cache_bar_cache_id_fkey FOREIGN KEY (cache_id) REFERENCES public.price_cache(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.price_cache
    ADD CONSTRAINT price_cache_security_id_fkey FOREIGN KEY (security_id) REFERENCES public.security(id);

ALTER TABLE ONLY public.request_receipt
    ADD CONSTRAINT request_receipt_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.batch(id);

ALTER TABLE ONLY public.request_scope
    ADD CONSTRAINT request_scope_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.batch(id);

ALTER TABLE ONLY public.request_scope
    ADD CONSTRAINT request_scope_security_id_fkey FOREIGN KEY (security_id) REFERENCES public.security(id);

ALTER TABLE ONLY public.research_track
    ADD CONSTRAINT research_track_latest_id_fkey FOREIGN KEY (latest_id) REFERENCES public.analysis_request(id);

ALTER TABLE ONLY public.research_track
    ADD CONSTRAINT research_track_origin_id_fkey FOREIGN KEY (origin_id) REFERENCES public.analysis_request(id);

ALTER TABLE ONLY public.security_identifier
    ADD CONSTRAINT security_identifier_security_id_fkey FOREIGN KEY (security_id) REFERENCES public.security(id);

ALTER TABLE ONLY public.security
    ADD CONSTRAINT security_issuer_id_fkey FOREIGN KEY (issuer_id) REFERENCES public.issuer(id);

ALTER TABLE ONLY public.source_observation
    ADD CONSTRAINT source_observation_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.job(id);

ALTER TABLE ONLY public.source_observation
    ADD CONSTRAINT source_observation_source_hash_fkey FOREIGN KEY (source_hash) REFERENCES public.source_object(sha256);

ALTER TABLE ONLY public.transaction_event
    ADD CONSTRAINT transaction_event_accession_fkey FOREIGN KEY (accession) REFERENCES public.filing(accession);

ALTER TABLE ONLY public.transaction_event
    ADD CONSTRAINT transaction_event_issuer_id_fkey FOREIGN KEY (issuer_id) REFERENCES public.issuer(id);

ALTER TABLE ONLY public.transaction_event
    ADD CONSTRAINT transaction_event_replaces_id_fkey FOREIGN KEY (replaces_id) REFERENCES public.transaction_event(id);

ALTER TABLE ONLY public.transaction_event
    ADD CONSTRAINT transaction_event_version_id_fkey FOREIGN KEY (version_id) REFERENCES public.filing_version(id);
