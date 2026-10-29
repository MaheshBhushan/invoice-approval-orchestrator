-- Safe to apply to an existing database; no reset or invoice deletion required.
CREATE TABLE IF NOT EXISTS approval_responses (
    invoice_id UUID PRIMARY KEY REFERENCES invoices (invoice_id),
    response JSONB NOT NULL CHECK (jsonb_typeof(response) = 'object'),
    delivered BOOLEAN NOT NULL DEFAULT FALSE,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
