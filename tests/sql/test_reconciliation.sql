\set ON_ERROR_STOP on
BEGIN;
INSERT INTO invoices (invoice_id, idempotency_key, supplier_id, purchase_order_id, supplier_invoice_number,
                      invoice_date, currency, net_amount, tax_amount, total_amount, status, raw_payload)
VALUES
    ('00000000-0000-0000-0000-0000000000b1', 'rc-ok', (SELECT supplier_id FROM suppliers WHERE supplier_code = 'SUP-001'),
     (SELECT purchase_order_id FROM purchase_orders WHERE po_number = 'PO-1001'), 'RC-1', '2026-08-01', 'EUR', 100, 19, 119, 'POSTED',
     '{"idempotency_key":"rc-ok","supplier_code":"SUP-001","purchase_order_number":"PO-1001","supplier_invoice_number":"RC-1","invoice_date":"2026-08-01","currency":"EUR","net_amount":100.00,"tax_amount":19,"total_amount":"119"}'),
    ('00000000-0000-0000-0000-0000000000b2', 'rc-bad', (SELECT supplier_id FROM suppliers WHERE supplier_code = 'SUP-001'),
     NULL, 'RC-2', '2026-08-01', 'EUR', 100, 19, 119, 'POSTED',
     '{"idempotency_key":"rc-bad","supplier_code":"SUP-001","purchase_order_number":"PO-1001","supplier_invoice_number":"RC-2","invoice_date":"2026-08-01","currency":"EUR","net_amount":100,"tax_amount":19,"total_amount":120}');
INSERT INTO finance_postings (invoice_id, idempotency_key, external_reference)
VALUES ('00000000-0000-0000-0000-0000000000b1', 'post-rc-ok', 'FIN-RC-1');

DO $$
DECLARE r record; n bigint;
BEGIN
    ASSERT (SELECT count(*) FROM reconciliation.rejected_records
            WHERE invoice_id = '00000000-0000-0000-0000-0000000000b1') = 0, 'matching invoice has no rejections';
    SELECT * INTO r FROM reconciliation.rejected_records
    WHERE invoice_id = '00000000-0000-0000-0000-0000000000b2' AND attribute = 'total_amount';
    ASSERT r.reason = 'value_mismatch' AND r.source_value = '120' AND r.target_value = '119.00', 'amount mismatch';
    ASSERT EXISTS (SELECT 1 FROM reconciliation.rejected_records WHERE invoice_id = '00000000-0000-0000-0000-0000000000b2'
                   AND attribute = 'purchase_order_number' AND reason = 'missing_target'), 'unresolved PO';
    ASSERT EXISTS (SELECT 1 FROM reconciliation.rejected_records WHERE invoice_id = '00000000-0000-0000-0000-0000000000b2'
                   AND reason = 'posted_without_posting'), 'posted without posting';

    SELECT * INTO r FROM reconciliation.attribute_coverage WHERE attribute = 'total_amount';
    ASSERT r.source_records = 2 AND r.matched_records = 1 AND r.coverage_ratio = 0.5000, 'attribute coverage';
    SELECT * INTO r FROM reconciliation.coverage_report;
    ASSERT r.source_invoices = 2 AND r.reconciled_invoices = 1 AND r.rejected_invoices = 1
       AND r.finance_postings = 1 AND r.coverage_ratio = 0.5000, 'coverage report';
    ASSERT (SELECT array_agg(idempotency_key) FROM reconciliation.publishable_results) = ARRAY['rc-ok'], 'publishable';

    BEGIN
        PERFORM * FROM reconciliation.publish_results();
        RAISE EXCEPTION 'gate did not block';
    EXCEPTION WHEN raise_exception THEN
        ASSERT SQLERRM LIKE 'reconciliation failed: 1 rejected%', SQLERRM;
    END;

    DELETE FROM invoices WHERE invoice_id = '00000000-0000-0000-0000-0000000000b2';
    SELECT count(*) INTO n FROM reconciliation.publish_results();
    ASSERT n = 1, 'gate publishes once reconciled';
    RAISE NOTICE 'reconciliation SQL assertions passed';
END;
$$;
ROLLBACK;
