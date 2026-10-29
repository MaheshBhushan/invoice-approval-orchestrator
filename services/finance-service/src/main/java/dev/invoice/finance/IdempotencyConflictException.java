package dev.invoice.finance;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.ResponseStatus;

@ResponseStatus(value = HttpStatus.CONFLICT, reason = "idempotency key belongs to another invoice")
public class IdempotencyConflictException extends RuntimeException {
}
