package dev.invoice.finance;

import jakarta.validation.Valid;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class PostingController {
    private final PostingService postings;

    public PostingController(PostingService postings) {
        this.postings = postings;
    }

    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", "ok");
    }

    @PostMapping("/postings")
    @ResponseStatus(HttpStatus.CREATED)
    public PostingResult create(@Valid @RequestBody PostingRequest request,
                                @RequestHeader("Idempotency-Key") String idempotencyKey) {
        return postings.post(request, idempotencyKey);
    }
}
