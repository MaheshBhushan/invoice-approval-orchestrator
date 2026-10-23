\set ON_ERROR_STOP on
BEGIN;
INSERT INTO invoices (invoice_id, idempotency_key, supplier_id, purchase_order_id, supplier_invoice_number,
                      invoice_date, currency, net_amount, tax_amount, total_amount, status,
                      agent_classification, agent_confidence, raw_payload, received_at)
VALUES
    ('00000000-0000-0000-0000-0000000000a1', 'dq-ok', (SELECT supplier_id FROM suppliers WHERE supplier_code = 'SUP-001'),
     (SELECT purchase_order_id FROM purchase_orders WHERE po_number = 'PO-1001'), 'DQ-1', '2026-08-01', 'EUR', 100, 19, 119, 'POSTED',
     'no_discrepancy', 0.95,
     '{"idempotency_key":"dq-ok","supplier_code":"SUP-001","purchase_order_number":"PO-1001","supplier_invoice_number":"DQ-1","invoice_date":"2026-08-01","currency":"EUR","net_amount":100,"tax_amount":19,"total_amount":119}',
     '2026-08-01 10:00:00+00'),
    ('00000000-0000-0000-0000-0000000000a2', 'dq-missing', NULL, NULL, 'DQ-2', '2026-08-01', 'EUR', 100, 19, 119,
     'VALIDATION_FAILED', NULL, NULL,
     '{"idempotency_key":"dq-missing","supplier_code":"","purchase_order_number":null,"supplier_invoice_number":"DQ-2","invoice_date":"2026-08-01","currency":"EUR","net_amount":100,"tax_amount":19,"total_amount":119}',
     '2026-08-01 10:00:00+00'),
    ('00000000-0000-0000-0000-0000000000a3', 'dq-mismatch', (SELECT supplier_id FROM suppliers WHERE supplier_code = 'SUP-001'),
     (SELECT purchase_order_id FROM purchase_orders WHERE po_number = 'PO-1001'), 'DQ-3', '2026-08-01', 'EUR', 1000, 190, 1190, 'PENDING_APPROVAL',
     'amount_mismatch', 0.9,
     '{"idempotency_key":"dq-mismatch","supplier_code":"SUP-001","purchase_order_number":"PO-1001","supplier_invoice_number":"DQ-3","invoice_date":"2026-08-01","currency":"EUR","net_amount":1000,"tax_amount":190,"total_amount":1190}',
     now() - interval '2 days');
INSERT INTO audit_events (invoice_id, event_type, actor_type, details)
VALUES ('00000000-0000-0000-0000-0000000000a2', 'VALIDATION_FAILED', 'SYSTEM', '{"classification":"unknown_supplier"}');
INSERT INTO process_metrics (invoice_id, started_at, completed_at, retry_count)
VALUES ('00000000-0000-0000-0000-0000000000a1', '2026-08-01 10:00:00+00', '2026-08-01 10:00:30+00', 2),
       ('00000000-0000-0000-0000-0000000000a2', '2026-08-01 10:00:00+00', '2026-08-01 10:05:00+00', 0);

DO $$
DECLARE r record;
BEGIN
    SELECT * INTO r FROM data_quality.missing_field_summary WHERE field_name = 'supplier_code';
    ASSERT r.missing_count = 1 AND r.invoice_ids = ARRAY['00000000-0000-0000-0000-0000000000a2'::uuid], 'empty supplier_code';
    SELECT * INTO r FROM data_quality.missing_field_summary WHERE field_name = 'purchase_order_number';
    ASSERT r.missing_count = 1, 'null purchase_order_number';
    SELECT * INTO r FROM data_quality.missing_field_summary WHERE field_name = 'supplier_id' AND source = 'invoices';
    ASSERT r.missing_count = 1 AND r.missing_ratio = 0.3333, 'unresolved supplier_id';
    ASSERT (SELECT count(*) FROM data_quality.missing_field_findings
            WHERE invoice_id <> '00000000-0000-0000-0000-0000000000a2') = 0, 'complete invoices have no findings';

    SELECT * INTO r FROM data_quality.discrepancy_findings WHERE invoice_id = '00000000-0000-0000-0000-0000000000a2';
    ASSERT r.category = 'unknown_supplier' AND r.category_source = 'validation' AND r.source_event_id IS NOT NULL,
        'validation category traced to audit event';
    SELECT * INTO r FROM data_quality.discrepancy_summary WHERE category = 'amount_mismatch';
    ASSERT r.invoice_count = 1 AND r.category_source = 'agent' AND r.avg_agent_confidence = 0.9000, 'agent category';

    SELECT * INTO r FROM data_quality.processing_delay_findings WHERE invoice_id = '00000000-0000-0000-0000-0000000000a1';
    ASSERT r.delay_seconds = 30 AND r.delay_bucket = 'under_1m' AND r.completed, 'completed delay';
    SELECT * INTO r FROM data_quality.processing_delay_findings WHERE invoice_id = '00000000-0000-0000-0000-0000000000a3';
    ASSERT NOT r.completed AND r.delay_bucket = 'over_1d', 'open invoice delay measured to now';
    SELECT * INTO r FROM data_quality.processing_delay_summary WHERE completed AND delay_bucket = 'under_1m';
    ASSERT r.invoice_count = 1 AND r.total_retries = 2 AND r.max_delay_seconds = 30, 'delay summary';
    RAISE NOTICE 'data quality SQL assertions passed';
END;
$$;
ROLLBACK;
