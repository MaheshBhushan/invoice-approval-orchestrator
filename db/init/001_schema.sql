CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE suppliers (
    supplier_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_code TEXT NOT NULL UNIQUE,
    legal_name TEXT NOT NULL,
    tax_id TEXT NOT NULL UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE purchase_orders (
    purchase_order_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    po_number TEXT NOT NULL UNIQUE,
    supplier_id UUID NOT NULL REFERENCES suppliers (supplier_id),
    currency CHAR(3) NOT NULL CHECK (currency = upper(currency)),
    total_amount NUMERIC(14, 2) NOT NULL CHECK (total_amount >= 0),
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED', 'CANCELLED')),
    valid_from DATE NOT NULL,
    valid_until DATE,
    CHECK (valid_until IS NULL OR valid_until >= valid_from)
);

CREATE TABLE invoices (
    invoice_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key TEXT NOT NULL UNIQUE,
    supplier_id UUID REFERENCES suppliers (supplier_id),
    purchase_order_id UUID REFERENCES purchase_orders (purchase_order_id),
    supplier_invoice_number TEXT NOT NULL,
    invoice_date DATE NOT NULL,
    currency CHAR(3) NOT NULL CHECK (currency = upper(currency)),
    net_amount NUMERIC(14, 2) NOT NULL CHECK (net_amount >= 0),
    tax_amount NUMERIC(14, 2) NOT NULL CHECK (tax_amount >= 0),
    total_amount NUMERIC(14, 2) NOT NULL CHECK (total_amount = net_amount + tax_amount),
    status TEXT NOT NULL DEFAULT 'RECEIVED' CHECK (
        status IN (
            'RECEIVED', 'EXTRACTED', 'VALIDATION_FAILED', 'CLASSIFIED',
            'PENDING_APPROVAL', 'APPROVED', 'REJECTED', 'POSTING',
            'POSTED', 'TECHNICAL_FAILURE'
        )
    ),
    agent_classification TEXT,
    agent_confidence NUMERIC(5, 4) CHECK (agent_confidence BETWEEN 0 AND 1),
    agent_explanation TEXT,
    raw_payload JSONB NOT NULL CHECK (jsonb_typeof(raw_payload) = 'object'),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (supplier_id, supplier_invoice_number)
);

CREATE TABLE processing_attempts (
    attempt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id UUID NOT NULL REFERENCES invoices (invoice_id),
    process_instance_key BIGINT,
    step TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    outcome TEXT NOT NULL CHECK (outcome IN ('STARTED', 'SUCCEEDED', 'RETRYABLE_FAILURE', 'BUSINESS_FAILURE')),
    error_code TEXT,
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    CHECK (finished_at IS NULL OR finished_at >= started_at),
    UNIQUE (invoice_id, step, attempt_number)
);

CREATE TABLE audit_events (
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    invoice_id UUID NOT NULL REFERENCES invoices (invoice_id),
    process_instance_key BIGINT,
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('SYSTEM', 'AGENT', 'HUMAN')),
    actor_id TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details) = 'object'),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE FUNCTION prevent_audit_event_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END;
$$;

CREATE TRIGGER audit_events_are_append_only
BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION prevent_audit_event_mutation();

CREATE TABLE finance_postings (
    posting_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id UUID NOT NULL UNIQUE REFERENCES invoices (invoice_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    external_reference TEXT UNIQUE,
    posted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    response_payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(response_payload) = 'object')
);

CREATE TABLE process_metrics (
    invoice_id UUID PRIMARY KEY REFERENCES invoices (invoice_id),
    process_instance_key BIGINT UNIQUE,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    straight_through BOOLEAN NOT NULL DEFAULT FALSE,
    human_review_count INTEGER NOT NULL DEFAULT 0 CHECK (human_review_count >= 0),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    CHECK (completed_at IS NULL OR completed_at >= started_at)
);

CREATE TABLE demo_invoice_cases (
    case_key TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    expected_route TEXT NOT NULL CHECK (expected_route IN ('AUTO_POST', 'BUSINESS_REJECTION', 'HUMAN_REVIEW', 'DUPLICATE')),
    expected_classification TEXT NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object')
);

CREATE INDEX invoices_status_idx ON invoices (status);
CREATE INDEX audit_events_invoice_time_idx ON audit_events (invoice_id, occurred_at);
CREATE INDEX processing_attempts_invoice_idx ON processing_attempts (invoice_id, started_at);

