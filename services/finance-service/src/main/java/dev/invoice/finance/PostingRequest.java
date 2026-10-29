package dev.invoice.finance;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;
import java.math.BigDecimal;
import java.util.UUID;

public record PostingRequest(
        @JsonProperty("invoice_id") @NotNull UUID invoiceId,
        @NotNull @Positive BigDecimal amount,
        @NotBlank String currency) {
}
