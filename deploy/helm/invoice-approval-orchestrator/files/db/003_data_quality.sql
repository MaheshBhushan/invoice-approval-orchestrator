-- Descriptive data quality statistics. Every finding view keeps the source
-- invoice_id (and audit event_id where relevant) so each aggregate can be
-- traced back to the input record that produced it.
CREATE SCHEMA IF NOT EXISTS data_quality;

-- One row per invoice and missing intake field or unresolved reference.
CREATE VIEW data_quality.missing_field_findings AS
SELECT i.invoice_id, i.idempotency_key, f.field_name, 'raw_payload'::text AS source
FROM invoices i
CROSS JOIN unnest(ARRAY[
    'idempotency_key', 'supplier_code', 'purchase_order_number', 'supplier_invoice_number',
    'invoice_date', 'currency', 'net_amount', 'tax_amount', 'total_amount'
]) AS f (field_name)
WHERE NOT (i.raw_payload ? f.field_name)
   OR jsonb_typeof(i.raw_payload -> f.field_name) = 'null'
   OR btrim(i.raw_payload ->> f.field_name) = ''
UNION ALL
SELECT i.invoice_id, i.idempotency_key, 'supplier_id', 'invoices'
FROM invoices i WHERE i.supplier_id IS NULL
UNION ALL
SELECT i.invoice_id, i.idempotency_key, 'purchase_order_id', 'invoices'
FROM invoices i WHERE i.purchase_order_id IS NULL;

CREATE VIEW data_quality.missing_field_summary AS
SELECT
    m.source,
    m.field_name,
    count(*) AS missing_count,
    round(count(*)::numeric / NULLIF((SELECT count(*) FROM invoices), 0), 4) AS missing_ratio,
    array_agg(m.invoice_id ORDER BY m.invoice_id) AS invoice_ids
FROM data_quality.missing_field_findings m
GROUP BY m.source, m.field_name;

-- Latest discrepancy category per invoice: validation failures from the audit
-- trail take precedence over the agent classification.
CREATE VIEW data_quality.discrepancy_findings AS
SELECT
    i.invoice_id,
    i.idempotency_key,
    i.status,
    CASE
        WHEN v.event_id IS NOT NULL THEN 'validation'
        WHEN i.agent_classification IS NOT NULL THEN 'agent'
        ELSE 'unclassified'
    END AS category_source,
    COALESCE(v.details ->> 'classification', i.agent_classification, 'unclassified') AS category,
    i.agent_confidence,
    v.event_id AS source_event_id
FROM invoices i
LEFT JOIN LATERAL (
    SELECT e.event_id, e.details
    FROM audit_events e
    WHERE e.invoice_id = i.invoice_id AND e.event_type = 'VALIDATION_FAILED'
    ORDER BY e.occurred_at DESC, e.event_id DESC
    LIMIT 1
) v ON TRUE;

CREATE VIEW data_quality.discrepancy_summary AS
SELECT
    d.category_source,
    d.category,
    count(*) AS invoice_count,
    round(avg(d.agent_confidence), 4) AS avg_agent_confidence,
    array_agg(d.invoice_id ORDER BY d.invoice_id) AS invoice_ids
FROM data_quality.discrepancy_findings d
GROUP BY d.category_source, d.category;

-- Processing delay from receipt to process completion; open invoices are
-- measured up to now() and flagged as not completed.
CREATE VIEW data_quality.processing_delay_findings AS
SELECT
    i.invoice_id,
    i.idempotency_key,
    i.status,
    i.received_at,
    pm.completed_at,
    pm.completed_at IS NOT NULL AS completed,
    COALESCE(pm.retry_count, 0) AS retry_count,
    EXTRACT(EPOCH FROM COALESCE(pm.completed_at, now()) - i.received_at)::numeric(14, 3) AS delay_seconds,
    CASE
        WHEN COALESCE(pm.completed_at, now()) - i.received_at < interval '1 minute' THEN 'under_1m'
        WHEN COALESCE(pm.completed_at, now()) - i.received_at < interval '1 hour' THEN '1m_to_1h'
        WHEN COALESCE(pm.completed_at, now()) - i.received_at < interval '1 day' THEN '1h_to_1d'
        ELSE 'over_1d'
    END AS delay_bucket
FROM invoices i
LEFT JOIN process_metrics pm ON pm.invoice_id = i.invoice_id;

CREATE VIEW data_quality.processing_delay_summary AS
SELECT
    p.completed,
    p.delay_bucket,
    count(*) AS invoice_count,
    round(avg(p.delay_seconds), 3) AS avg_delay_seconds,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY p.delay_seconds)::numeric, 3) AS p50_delay_seconds,
    round(percentile_cont(0.95) WITHIN GROUP (ORDER BY p.delay_seconds)::numeric, 3) AS p95_delay_seconds,
    max(p.delay_seconds) AS max_delay_seconds,
    sum(p.retry_count) AS total_retries,
    array_agg(p.invoice_id ORDER BY p.invoice_id) AS invoice_ids
FROM data_quality.processing_delay_findings p
GROUP BY p.completed, p.delay_bucket;
