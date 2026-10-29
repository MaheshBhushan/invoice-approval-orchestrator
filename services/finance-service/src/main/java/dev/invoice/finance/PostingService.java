package dev.invoice.finance;

import java.util.List;
import java.util.Locale;
import java.util.UUID;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

/**
 * Posts each invoice at most once. The UNIQUE constraints on finance_postings.invoice_id and
 * finance_postings.idempotency_key decide the winner of concurrent deliveries; losers read back
 * the committed posting instead of creating a second one.
 */
@Service
public class PostingService {
    private final JdbcTemplate jdbc;

    public PostingService(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public PostingResult post(PostingRequest request, String idempotencyKey) {
        String reference = "FIN-" + UUID.randomUUID().toString().replace("-", "").substring(0, 12).toUpperCase(Locale.ROOT);
        try {
            jdbc.update(
                    "INSERT INTO finance_postings (invoice_id, idempotency_key, external_reference, response_payload) "
                            + "VALUES (?, ?, ?, CAST(? AS jsonb))",
                    request.invoiceId(), idempotencyKey, reference,
                    "{\"external_reference\": \"" + reference + "\"}");
            return new PostingResult(reference, false);
        } catch (DuplicateKeyException duplicate) {
            List<Existing> rows = jdbc.query(
                    "SELECT invoice_id, external_reference FROM finance_postings WHERE idempotency_key = ? OR invoice_id = ?",
                    (rs, i) -> new Existing(rs.getObject("invoice_id", UUID.class), rs.getString("external_reference")),
                    idempotencyKey, request.invoiceId());
            if (rows.size() != 1 || !rows.get(0).invoiceId().equals(request.invoiceId())) {
                throw new IdempotencyConflictException();
            }
            return new PostingResult(rows.get(0).externalReference(), true);
        }
    }

    private record Existing(UUID invoiceId, String externalReference) {
    }
}
