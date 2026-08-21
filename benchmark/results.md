# Invoice workflow benchmark

Run `4c710baa` processed 100 invoices on 2026-08-21T12:34:14Z.

| Metric | Result |
|---|---:|
| Completed | 100 / 100 |
| Mean completion latency | 5611.25 ms |
| p50 completion latency | 6795.62 ms |
| p95 completion latency | 7603.66 ms |
| Straight-through-processing rate | 90.0% |
| Human-review rate | 10.0% |
| Duplicate deliveries prevented | 10 |
| Durable retries recorded | 0 |
| Classification accuracy | 100.0% |

Ground truth is deterministic seeded data: exact PO match, 19% amount mismatch, and ambiguous surcharge.
