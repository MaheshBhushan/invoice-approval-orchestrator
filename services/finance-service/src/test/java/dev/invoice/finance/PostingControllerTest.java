package dev.invoice.finance;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.stream.IntStream;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;

@SpringBootTest
@AutoConfigureMockMvc
class PostingControllerTest {
    @Autowired MockMvc mvc;
    @Autowired PostingService postings;
    @Autowired JdbcTemplate jdbc;
    @Autowired ObjectMapper json;

    private ResultActions send(UUID invoiceId, String key) throws Exception {
        return mvc.perform(post("/postings")
                .contentType(MediaType.APPLICATION_JSON)
                .header("Idempotency-Key", key)
                .content("{\"invoice_id\":\"" + invoiceId + "\",\"amount\":1000.00,\"currency\":\"EUR\"}"));
    }

    private JsonNode body(ResultActions result) throws Exception {
        return json.readTree(result.andReturn().getResponse().getContentAsString());
    }

    private int count(UUID invoiceId) {
        return jdbc.queryForObject("SELECT count(*) FROM finance_postings WHERE invoice_id = ?", Integer.class, invoiceId);
    }

    @Test
    void healthReportsOk() throws Exception {
        mvc.perform(get("/health")).andExpect(status().isOk()).andExpect(jsonPath("$.status").value("ok"));
    }

    @Test
    void redeliveryReturnsOriginalPosting() throws Exception {
        UUID invoice = UUID.randomUUID();
        JsonNode first = body(send(invoice, "invoice:" + invoice).andExpect(status().isCreated()));
        assertThat(first.get("reused").asBoolean()).isFalse();
        assertThat(first.get("external_reference").asText()).matches("FIN-[0-9A-F]{12}");

        JsonNode again = body(send(invoice, "invoice:" + invoice).andExpect(status().isCreated()));
        assertThat(again.get("reused").asBoolean()).isTrue();
        assertThat(again.get("external_reference").asText()).isEqualTo(first.get("external_reference").asText());

        JsonNode newKey = body(send(invoice, "retry:" + invoice).andExpect(status().isCreated()));
        assertThat(newKey.get("external_reference").asText()).isEqualTo(first.get("external_reference").asText());
        assertThat(count(invoice)).isEqualTo(1);
    }

    @Test
    void keyReusedForAnotherInvoiceIsRejected() throws Exception {
        UUID invoice = UUID.randomUUID();
        send(invoice, "shared:" + invoice).andExpect(status().isCreated());
        UUID other = UUID.randomUUID();
        send(other, "shared:" + invoice).andExpect(status().isConflict());
        assertThat(count(other)).isZero();
    }

    @Test
    void invalidRequestsAreRejected() throws Exception {
        mvc.perform(post("/postings").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"invoice_id\":\"" + UUID.randomUUID() + "\",\"amount\":1,\"currency\":\"EUR\"}"))
                .andExpect(status().isBadRequest());
        mvc.perform(post("/postings").contentType(MediaType.APPLICATION_JSON).header("Idempotency-Key", "k")
                        .content("{\"invoice_id\":\"" + UUID.randomUUID() + "\",\"amount\":-5,\"currency\":\"EUR\"}"))
                .andExpect(status().isBadRequest());
    }

    @Test
    void concurrentFirstDeliveriesShareOneReference() throws Exception {
        UUID invoice = UUID.randomUUID();
        PostingRequest request = new PostingRequest(invoice, new java.math.BigDecimal("1000.00"), "EUR");
        List<Callable<PostingResult>> calls = IntStream.range(0, 24)
                .<Callable<PostingResult>>mapToObj(i -> () -> postings.post(request, "invoice:" + invoice))
                .toList();
        ExecutorService pool = Executors.newFixedThreadPool(8);
        try {
            List<PostingResult> results = pool.invokeAll(calls).stream().map(PostingControllerTest::join).toList();
            assertThat(results.stream().map(PostingResult::externalReference).distinct()).hasSize(1);
            assertThat(results.stream().filter(r -> !r.reused())).hasSize(1);
        } finally {
            pool.shutdown();
        }
        assertThat(count(invoice)).isEqualTo(1);
    }

    private static PostingResult join(Future<PostingResult> future) {
        try {
            return future.get();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
