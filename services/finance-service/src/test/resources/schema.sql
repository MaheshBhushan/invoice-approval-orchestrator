CREATE DOMAIN IF NOT EXISTS jsonb AS JSON;
CREATE TABLE finance_postings (
    posting_id UUID DEFAULT RANDOM_UUID() PRIMARY KEY,
    invoice_id UUID NOT NULL UNIQUE,
    idempotency_key VARCHAR NOT NULL UNIQUE,
    external_reference VARCHAR UNIQUE,
    posted_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    response_payload jsonb NOT NULL
);
