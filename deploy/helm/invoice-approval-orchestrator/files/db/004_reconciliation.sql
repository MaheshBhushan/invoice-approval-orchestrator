-- Source-to-target reconciliation. The intake payload (invoices.raw_payload)
-- is the source; the typed invoice columns and finance_postings are the
-- persisted target. Downstream consumers read reconciliation.publishable_results,
-- which only exposes invoices without rejected-record findings.
CREATE SCHEMA IF NOT EXISTS reconciliation;

-- One row per invoice and required attribute, comparing source and target.
CREATE VIEW reconciliation.attribute_checks AS
SELECT
    i.invoice_id,
    i.idempotency_key,
    a.attribute,
    i.raw_payload ->> a.attribute AS source_value,
    a.target_value,
    CASE
        WHEN NOT (i.raw_payload ? a.attribute)
          OR jsonb_typeof(i.raw_payload -> a.attribute) = 'null'
          OR btrim(i.raw_payload ->> a.attribute) = '' THEN 'missing_source'
        WHEN a.target_value IS NULL THEN 'missing_target'
        WHEN a.numeric_compare AND (i.raw_payload ->> a.attribute) !~ '^-?[0-9]+(\.[0-9]+)?$' THEN 'value_mismatch'
        WHEN a.numeric_compare AND (i.raw_payload ->> a.attribute)::numeric <> a.target_value::numeric THEN 'value_mismatch'
        WHEN NOT a.numeric_compare AND btrim(i.raw_payload ->> a.attribute) <> a.target_value THEN 'value_mismatch'
        ELSE 'matched'
    END AS result
FROM invoices i
CROSS JOIN LATERAL (VALUES
    ('idempotency_key', i.idempotency_key, FALSE),
    ('supplier_code', (SELECT s.supplier_code FROM suppliers s WHERE s.supplier_id = i.supplier_id), FALSE),
    ('purchase_order_number', (SELECT p.po_number FROM purchase_orders p WHERE p.purchase_order_id = i.purchase_order_id), FALSE),
    ('supplier_invoice_number', i.supplier_invoice_number, FALSE),
    ('invoice_date', i.invoice_date::text, FALSE),
    ('currency', i.currency::text, FALSE),
    ('net_amount', i.net_amount::text, TRUE),
    ('tax_amount', i.tax_amount::text, TRUE),
    ('total_amount', i.total_amount::text, TRUE)
) AS a (attribute, target_value, numeric_compare);

-- Rejected records: attribute discrepancies plus processing results that
-- disagree with the persisted finance postings.
CREATE VIEW reconciliation.rejected_records AS
SELECT c.invoice_id, c.idempotency_key, c.result AS reason, c.attribute, c.source_value, c.target_value
FROM reconciliation.attribute_checks c
WHERE c.result <> 'matched'
UNION ALL
SELECT i.invoice_id, i.idempotency_key, 'posted_without_posting', 'finance_posting', i.status, NULL
FROM invoices i
WHERE i.status = 'POSTED'
  AND NOT EXISTS (SELECT 1 FROM finance_postings f WHERE f.invoice_id = i.invoice_id)
UNION ALL
SELECT i.invoice_id, i.idempotency_key, 'posting_without_posted_status', 'finance_posting', i.status, f.external_reference
FROM invoices i
JOIN finance_postings f ON f.invoice_id = i.invoice_id
WHERE i.status <> 'POSTED';

-- Coverage per required attribute across all source invoices.
CREATE VIEW reconciliation.attribute_coverage AS
SELECT
    c.attribute,
    count(*) AS source_records,
    count(*) FILTER (WHERE c.result = 'matched') AS matched_records,
    count(*) FILTER (WHERE c.result <> 'matched') AS rejected_records,
    round(count(*) FILTER (WHERE c.result = 'matched')::numeric / NULLIF(count(*), 0), 4) AS coverage_ratio
FROM reconciliation.attribute_checks c
GROUP BY c.attribute;

-- Overall coverage of source identifiers by reconciled target records.
CREATE VIEW reconciliation.coverage_report AS
SELECT
    count(*) AS source_invoices,
    count(*) FILTER (WHERE NOT r.rejected) AS reconciled_invoices,
    count(*) FILTER (WHERE r.rejected) AS rejected_invoices,
    count(*) FILTER (WHERE i.status = 'POSTED') AS posted_invoices,
    count(f.invoice_id) AS finance_postings,
    round(count(*) FILTER (WHERE NOT r.rejected)::numeric / NULLIF(count(*), 0), 4) AS coverage_ratio
FROM invoices i
LEFT JOIN finance_postings f ON f.invoice_id = i.invoice_id
CROSS JOIN LATERAL (
    SELECT EXISTS (SELECT 1 FROM reconciliation.rejected_records x WHERE x.invoice_id = i.invoice_id) AS rejected
) r;

-- Downstream publication: posted results whose invoice fully reconciles.
CREATE VIEW reconciliation.publishable_results AS
SELECT i.invoice_id, i.idempotency_key, i.supplier_invoice_number, i.currency, i.total_amount,
       f.external_reference, f.posted_at
FROM invoices i
JOIN finance_postings f ON f.invoice_id = i.invoice_id
WHERE i.status = 'POSTED'
  AND NOT EXISTS (SELECT 1 FROM reconciliation.rejected_records x WHERE x.invoice_id = i.invoice_id);

-- Publication gate: raises when any rejected record exists, otherwise
-- returns the publishable results.
CREATE FUNCTION reconciliation.publish_results()
RETURNS SETOF reconciliation.publishable_results
LANGUAGE plpgsql
AS $$
DECLARE rejected bigint;
BEGIN
    SELECT count(DISTINCT invoice_id) INTO rejected FROM reconciliation.rejected_records;
    IF rejected > 0 THEN
        RAISE EXCEPTION 'reconciliation failed: % rejected invoice(s); see reconciliation.rejected_records', rejected;
    END IF;
    RETURN QUERY SELECT * FROM reconciliation.publishable_results;
END;
$$;
