INSERT INTO suppliers (supplier_id, supplier_code, legal_name, tax_id, is_active)
VALUES
    ('10000000-0000-0000-0000-000000000001', 'SUP-001', 'Acme Office Supplies GmbH', 'DE123456789', TRUE),
    ('10000000-0000-0000-0000-000000000002', 'SUP-002', 'Globex Logistics GmbH', 'DE987654321', TRUE);

INSERT INTO purchase_orders (
    purchase_order_id, po_number, supplier_id, currency, total_amount, status, valid_from, valid_until
)
VALUES
    (
        '20000000-0000-0000-0000-000000000001', 'PO-1001',
        '10000000-0000-0000-0000-000000000001', 'EUR', 1000.00, 'OPEN', '2026-01-01', '2026-12-31'
    ),
    (
        '20000000-0000-0000-0000-000000000002', 'PO-2001',
        '10000000-0000-0000-0000-000000000002', 'EUR', 2500.00, 'OPEN', '2026-01-01', '2026-12-31'
    );

INSERT INTO demo_invoice_cases (case_key, description, expected_route, expected_classification, payload)
VALUES
    (
        'valid', 'Supplier and totals match an open purchase order', 'AUTO_POST', 'no_discrepancy',
        '{"idempotency_key":"demo-valid-001","supplier_code":"SUP-001","purchase_order_number":"PO-1001","supplier_invoice_number":"INV-2026-0001","invoice_date":"2026-08-01","currency":"EUR","net_amount":840.34,"tax_amount":159.66,"total_amount":1000.00}'
    ),
    (
        'amount-mismatch', 'Invoice exceeds its purchase order by 19 percent', 'AUTO_POST', 'amount_mismatch',
        '{"idempotency_key":"demo-mismatch-001","supplier_code":"SUP-001","purchase_order_number":"PO-1001","supplier_invoice_number":"INV-2026-0002","invoice_date":"2026-08-02","currency":"EUR","net_amount":1000.00,"tax_amount":190.00,"total_amount":1190.00}'
    ),
    (
        'unknown-supplier', 'Supplier does not exist in the supplier master', 'BUSINESS_REJECTION', 'unknown_supplier',
        '{"idempotency_key":"demo-unknown-001","supplier_code":"SUP-999","purchase_order_number":"PO-1001","supplier_invoice_number":"INV-2026-0003","invoice_date":"2026-08-03","currency":"EUR","net_amount":840.34,"tax_amount":159.66,"total_amount":1000.00}'
    ),
    (
        'duplicate', 'Redelivery of the valid invoice uses the same idempotency key and invoice number', 'DUPLICATE', 'duplicate_invoice',
        '{"idempotency_key":"demo-valid-001","supplier_code":"SUP-001","purchase_order_number":"PO-1001","supplier_invoice_number":"INV-2026-0001","invoice_date":"2026-08-01","currency":"EUR","net_amount":840.34,"tax_amount":159.66,"total_amount":1000.00}'
    ),
    (
        'ambiguous', 'Small total variance and an unclear service surcharge require human judgment', 'HUMAN_REVIEW', 'ambiguous_discrepancy',
        '{"idempotency_key":"demo-ambiguous-001","supplier_code":"SUP-002","purchase_order_number":"PO-2001","supplier_invoice_number":"INV-2026-0004","invoice_date":"2026-08-04","currency":"EUR","net_amount":2113.45,"tax_amount":401.55,"total_amount":2515.00,"line_notes":["Service surcharge per verbal agreement"]}'
    );

