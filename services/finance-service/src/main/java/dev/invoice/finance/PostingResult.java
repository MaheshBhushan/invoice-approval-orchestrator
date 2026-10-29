package dev.invoice.finance;

import com.fasterxml.jackson.annotation.JsonProperty;

public record PostingResult(@JsonProperty("external_reference") String externalReference, boolean reused) {
}
