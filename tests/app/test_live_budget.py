import asyncio
import hashlib
import json
from decimal import Decimal

import httpx
import pytest

from studynavigator.providers import ProviderError
from tools.live_budget import BudgetTransport


URL = "https://ai.api.cloud.yandex.net/v1/chat/completions"


def payload(**overrides):
    return {"model": "gpt://test-folder/yandexgpt-5.1/latest",
            "messages": [{"role": "user", "content": "Учебный вопрос"}],
            "max_tokens": 20, **overrides}


def success():
    return httpx.Response(200, json={"choices": [{"message": {"content": "Ответ"}}],
                                   "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}})


def tokenized(count=80, **overrides):
    return {"tokens": [{"id": "1", "text": "x", "special": False}] * count,
            "modelVersion": "yagpt-5.1-2025-08", **overrides}


@pytest.mark.parametrize("budget", [0, -1, 500.01, float("nan"), float("inf"), True])
def test_guard_rejects_invalid_or_above_500_budget_without_dispatch(tmp_path, budget):
    with pytest.raises(ProviderError) as caught:
        BudgetTransport(tmp_path / "invalid.json", budget_rub=budget)
    assert caught.value.code == "eval_budget_invalid"
    assert not (tmp_path / "invalid.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("ceiling", ["476.9223", "500"])
async def test_server_ceiling_requires_explicit_ledger_migration_and_preserves_history_and_unknown_reserve(tmp_path, ceiling):
    ledger = tmp_path / "ledger.json"
    old_attempts = [
        {"id": 1, "model": "yandexgpt-5.1/latest", "status": "actual", "reserved_rub": "12.0",
         "prompt_tokens_upper_bound": 14000, "completion_tokens_upper_bound": 1000,
         "usage": {"prompt_tokens": 12000, "completion_tokens": 500, "total_tokens": 12500},
         "actual_cost_rub": "10.0"},
        {"id": 2, "model": "aliceai-llm-flash", "status": "unknown", "reserved_rub": "0.5",
         "prompt_tokens_upper_bound": 3000, "completion_tokens_upper_bound": 1000,
         "input_rate_rub_per_1000_tokens": "0.1", "output_rate_rub_per_1000_tokens": "0.2",
         "reason": "transport_or_read_error"},
    ]
    original = {"version": 1, "budget_rub": "18.7011", "rate_rub_per_1000_tokens": "0.8",
                "attempts": old_attempts, "blocked": True, "blocked_reason": "insufficient_reservation",
                "last_blocked_request": {"model": "yandexgpt-5.1/latest", "reserved_rub": "20"}}
    ledger.write_text(json.dumps(original))
    before = ledger.read_bytes()
    with pytest.raises(ProviderError) as caught:
        BudgetTransport(ledger, budget_rub=ceiling)
    assert caught.value.code == "eval_budget_invalid"
    assert ledger.read_bytes() == before  # Settings never silently grant or migrate funds.

    extension = {"old_budget_rub": "18.7011", "new_budget_rub": ceiling, "authorized_server_ceiling_rub": ceiling}
    migrated = {**original, "budget_rub": ceiling, "blocked": False, "blocked_reason": None,
                "budget_extensions": [extension],
                "reservation_recoveries": [{"previous_reason": original["blocked_reason"],
                                            "last_blocked_request": original["last_blocked_request"]}]}
    ledger.write_text(json.dumps(migrated))  # Simulated audited admin migration, never application code.
    transport = BudgetTransport(ledger, budget_rub=ceiling, inner=httpx.MockTransport(lambda request: success()))
    assert transport.summary()["actual_cost_rub"] == 10
    assert transport.summary()["unknown_reserved_rub"] == 0.5
    assert transport.summary()["remaining_rub"] == float(Decimal(ceiling) - Decimal("10.5"))
    async with httpx.AsyncClient(transport=transport) as client:
        assert (await client.post(URL, json=payload())).status_code == 200
    saved = json.loads(ledger.read_text())
    assert saved["attempts"][:2] == old_attempts
    assert saved["budget_extensions"] == [extension]
    assert saved["reservation_recoveries"] == migrated["reservation_recoveries"]
    assert saved["last_blocked_request"] == original["last_blocked_request"]
    summary = transport.summary()
    assert summary["attempts"] == 3
    assert summary["actual_cost_rub"] == pytest.approx(10.0128)
    assert summary["unknown_reserved_rub"] == 0.5
    assert summary["remaining_rub"] == float(Decimal(ceiling) - Decimal("10.5128"))
    await transport.close()
    reloaded = BudgetTransport(ledger, budget_rub=ceiling)
    assert reloaded.summary() == summary
    await reloaded.close()


@pytest.mark.asyncio
async def test_reservation_is_saved_before_dispatch_then_reconciled(tmp_path):
    ledger = tmp_path / "ledger.json"

    def dispatch(request):
        state = json.loads(ledger.read_text())
        assert state["attempts"][0]["status"] == "pending"
        assert float(state["attempts"][0]["reserved_rub"]) > 1
        return success()

    transport = BudgetTransport(ledger, inner=httpx.MockTransport(dispatch))
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.post(URL, json=payload())
    assert response.status_code == 200
    assert transport.summary()["actual_cost_rub"] == pytest.approx(0.0128)
    assert transport.summary()["unknown_reserved_rub"] == 0
    await transport.close()


@pytest.mark.asyncio
async def test_each_retry_is_accounted_and_first_error_is_not_refunded(tmp_path):
    count = 0

    def dispatch(request):
        nonlocal count
        count += 1
        return httpx.Response(503, json={"error": "Temporary"}) if count == 1 else success()

    transport = BudgetTransport(tmp_path / "ledger.json", inner=httpx.MockTransport(dispatch))
    async with httpx.AsyncClient(transport=transport) as client:
        assert (await client.post(URL, json=payload())).status_code == 503
        first_reservation = transport.summary()["unknown_reserved_rub"]
        assert (await client.post(URL, json=payload())).status_code == 200
    state = transport.summary()
    assert state["attempts"] == 2
    assert state["accounted_rub"] == pytest.approx(first_reservation + 0.0128)
    await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"choices": []}, {"usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 1}}])
async def test_success_without_valid_usage_keeps_the_reservation(tmp_path, body):
    transport = BudgetTransport(tmp_path / "ledger.json", inner=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post(URL, json=payload())
    state = transport.summary()
    assert state["actual_cost_rub"] == 0
    assert state["unknown_reserved_rub"] > 1
    assert state["remaining_rub"] < state["budget_rub"]
    await transport.close()


@pytest.mark.asyncio
async def test_timeout_still_consumes_a_persistent_reservation(tmp_path):
    ledger = tmp_path / "ledger.json"

    def timeout(request):
        raise httpx.ReadTimeout("No response", request=request)

    transport = BudgetTransport(ledger, inner=httpx.MockTransport(timeout))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.post(URL, json=payload())
    previous = transport.summary()
    await transport.close()
    reloaded = BudgetTransport(ledger, inner=httpx.MockTransport(lambda request: success()))
    assert reloaded.summary() == previous
    assert previous["unknown_reserved_rub"] > 0
    await reloaded.close()


@pytest.mark.asyncio
async def test_cancelled_dispatch_keeps_its_reservation(tmp_path):
    started = asyncio.Event()

    async def pending(request):
        started.set()
        await asyncio.Event().wait()

    transport = BudgetTransport(tmp_path / "ledger.json", inner=httpx.MockTransport(pending))
    async with httpx.AsyncClient(transport=transport) as client:
        operation = asyncio.create_task(client.post(URL, json=payload()))
        await asyncio.wait_for(started.wait(), 1)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
    assert transport.summary()["unknown_reserved_rub"] > 0
    await transport.close()


@pytest.mark.asyncio
async def test_insufficient_budget_never_dispatches_and_stays_blocked_on_restart(tmp_path):
    calls = []
    ledger = tmp_path / "ledger.json"
    transport = BudgetTransport(ledger, budget_rub=0.01, inner=httpx.MockTransport(lambda request: calls.append(request) or success()))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderError) as error:
            await client.post(URL, json=payload())
    assert error.value.code == "eval_budget_exceeded"
    assert not calls
    assert transport.summary()["attempts"] == 0
    await transport.close()
    reloaded = BudgetTransport(ledger, budget_rub=0.01, inner=httpx.MockTransport(lambda request: calls.append(request) or success()))
    assert reloaded.summary()["blocked"]
    await reloaded.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [{"model": "gpt://test-folder/another-model"},
                                      {"model": "gpt://test-folder/aliceai-llm"}, {"n": 2}, {"stream": True},
                                      {"messages": [{"role": "user", "content": "x"}] * 31},
                                      {"tools": [{}] * 4}, {"max_tokens": True}])
async def test_unpriced_or_unbounded_request_is_rejected_before_dispatch(tmp_path, overrides):
    calls = []
    transport = BudgetTransport(tmp_path / "ledger.json", inner=httpx.MockTransport(lambda request: calls.append(request) or success()))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderError):
            await client.post(URL, json=payload(**overrides))
    assert not calls
    await transport.close()


@pytest.mark.asyncio
async def test_usage_exceeding_estimate_is_recorded_and_stops_future_dispatches(tmp_path):
    calls = []

    def dispatch(request):
        calls.append(request)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 10, "completion_tokens": 21, "total_tokens": 31}})

    transport = BudgetTransport(tmp_path / "ledger.json", inner=httpx.MockTransport(dispatch))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderError):
            await client.post(URL, json=payload())
        with pytest.raises(ProviderError):
            await client.post(URL, json=payload())
    assert len(calls) == 1
    state = transport.summary()
    assert state["actual_cost_rub"] == pytest.approx(31 * 0.0008)
    assert state["blocked_reason"] == "usage_exceeded_reservation"
    await transport.close()


@pytest.mark.asyncio
async def test_ledger_never_contains_headers_prompt_or_response_bodies(tmp_path):
    ledger = tmp_path / "ledger.json"
    transport = BudgetTransport(ledger, inner=httpx.MockTransport(lambda request: httpx.Response(503, json={"error": "private-response"})))
    async with httpx.AsyncClient(transport=transport, headers={"Authorization": "Api-Key private-key"}) as client:
        await client.post(URL, json=payload(messages=[{"role": "user", "content": "private-prompt"}]))
    saved = ledger.read_text()
    for secret in ("private-key", "private-prompt", "private-response", "test-folder"):
        assert secret not in saved
    await transport.close()


@pytest.mark.asyncio
async def test_short_lived_provider_clients_can_reuse_the_same_transport(tmp_path):
    transport = BudgetTransport(tmp_path / "ledger.json", inner=httpx.MockTransport(lambda request: success()))
    for _ in range(2):
        async with httpx.AsyncClient(transport=transport) as client:
            await client.post(URL, json=payload())
    assert transport.summary()["attempts"] == 2
    await transport.close()


@pytest.mark.asyncio
async def test_owned_connection_pools_close_inside_each_client_and_reopen(monkeypatch, tmp_path):
    pools = []

    class Pool(httpx.AsyncBaseTransport):
        def __init__(self, **kwargs):
            self.closed = False
            pools.append(self)

        async def handle_async_request(self, request):
            assert not self.closed
            return success()

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", Pool)
    transport = BudgetTransport(tmp_path / "ledger.json")
    for _ in range(2):
        async with httpx.AsyncClient(transport=transport) as client:
            await client.post(URL, json=payload())
        assert transport.inner is None
    assert len(pools) == 2 and all(pool.closed for pool in pools)
    await transport.close()


@pytest.mark.asyncio
async def test_optional_tokenizer_counts_literal_tools_and_persists_bound_metadata(tmp_path):
    ledger = tmp_path / "ledger.json"
    paid_bodies = []
    native_texts = []
    tools = [{"type": "function", "function": {"name": "search", "description": "x" * 3000,
                                               "parameters": {"type": "object"}}}]

    def dispatch(request):
        if request.url.path.endswith("tokenizeCompletion"):
            native = json.loads(request.content)
            assert native["modelUri"] == payload()["model"]
            assert "tools" not in native
            literal = native["messages"][0]["text"]
            native_texts.append(literal)
            assert json.loads(literal)["tools"] == tools
            return httpx.Response(200, json=tokenized())
        paid_bodies.append(request.content)
        return success()

    transport = BudgetTransport(ledger, budget_rub=1, inner=httpx.MockTransport(dispatch), tokenize_payload=True)
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post(URL, json=payload(tools=tools))
    assert len(native_texts) == len(paid_bodies) == 1
    assert native_texts[0].encode("utf-8") == paid_bodies[0]
    attempt = json.loads(ledger.read_text())["attempts"][0]
    assert attempt["estimate_strategy"] == "literal_payload_tokenizer_plus_1024"
    assert attempt["native_input_tokens"] == 80
    assert attempt["prompt_tokens_upper_bound"] == 1104
    assert attempt["tokenizer_model_version"] == "yagpt-5.1-2025-08"
    assert attempt["payload_sha256"] == hashlib.sha256(paid_bodies[0]).hexdigest()
    assert transport.summary()["actual_cost_rub"] == pytest.approx(0.0128)
    await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("native_result", [
    tokenized(modelUri="gpt://different-folder/yandexgpt-5.1/latest"),
    tokenized(modelVersion=""),
    tokenized(tokens=[]),
    tokenized(tokens=[{"id": "1", "text": "x", "special": "false"}]),
    tokenized(tokens=[{"id": False, "text": "x", "special": False}]),
    tokenized(count=32_768),
])
async def test_invalid_tokenizer_results_fall_back_to_the_original_byte_bound(tmp_path, native_result):
    ledger = tmp_path / "ledger.json"
    paid_bodies = []

    def dispatch(request):
        if request.url.path.endswith("tokenizeCompletion"):
            return httpx.Response(200, json=native_result)
        paid_bodies.append(request.content)
        return success()

    transport = BudgetTransport(ledger, inner=httpx.MockTransport(dispatch), tokenize_payload=True)
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post(URL, json=payload())
    attempt = json.loads(ledger.read_text())["attempts"][0]
    assert attempt["estimate_strategy"] == "utf8_bytes"
    assert attempt["prompt_tokens_upper_bound"] == len(paid_bodies[0]) + 2048
    assert attempt["tokenizer_status"] == "unavailable_or_invalid"
    await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "timeout"])
async def test_free_tokenizer_failure_cannot_bypass_an_insufficient_budget(tmp_path, failure):
    paid = []

    def dispatch(request):
        if request.url.path.endswith("tokenizeCompletion"):
            if failure == "timeout":
                raise httpx.ReadTimeout("Synthetic tokenizer timeout", request=request)
            return httpx.Response(503, json={"error": "unavailable"})
        paid.append(request)
        return success()

    transport = BudgetTransport(tmp_path / "ledger.json", budget_rub=1,
                                inner=httpx.MockTransport(dispatch), tokenize_payload=True)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderError) as error:
            await client.post(URL, json=payload())
    assert error.value.code == "eval_budget_exceeded"
    assert not paid
    assert transport.summary()["accounted_rub"] == 0
    assert transport.summary()["blocked"]
    await transport.close()


@pytest.mark.asyncio
async def test_tokenizer_is_repeated_and_reserved_for_each_paid_http_attempt(tmp_path):
    paid = []
    free = []

    def dispatch(request):
        if request.url.path.endswith("tokenizeCompletion"):
            free.append(request)
            return httpx.Response(200, json=tokenized())
        paid.append(request)
        return httpx.Response(503, json={"error": "temporary"}) if len(paid) == 1 else success()

    transport = BudgetTransport(tmp_path / "ledger.json", inner=httpx.MockTransport(dispatch), tokenize_payload=True)
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post(URL, json=payload())
        first_reservation = transport.summary()["unknown_reserved_rub"]
        await client.post(URL, json=payload())
    assert len(free) == len(paid) == 2
    assert first_reservation == pytest.approx((80 + 1024 + 20) * 0.0008)
    assert transport.summary()["accounted_rub"] == pytest.approx(first_reservation + 0.0128)
    await transport.close()


@pytest.mark.asyncio
async def test_usage_above_tokenizer_bound_stops_both_future_free_and_paid_calls(tmp_path):
    paths = []

    def dispatch(request):
        paths.append(request.url.path)
        if request.url.path.endswith("tokenizeCompletion"):
            return httpx.Response(200, json=tokenized())
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1200, "completion_tokens": 4, "total_tokens": 1204}})

    transport = BudgetTransport(tmp_path / "ledger.json", inner=httpx.MockTransport(dispatch), tokenize_payload=True)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderError):
            await client.post(URL, json=payload())
        with pytest.raises(ProviderError):
            await client.post(URL, json=payload())
    assert paths == ["/foundationModels/v1/tokenizeCompletion", "/v1/chat/completions"]
    assert transport.summary()["blocked_reason"] == "usage_exceeded_reservation"
    await transport.close()


@pytest.mark.asyncio
async def test_cancellation_during_free_tokenization_does_not_dispatch_paid_call(tmp_path):
    started = asyncio.Event()
    paid = []

    async def pending(request):
        if request.url.path.endswith("tokenizeCompletion"):
            started.set()
            await asyncio.Event().wait()
        paid.append(request)
        return success()

    transport = BudgetTransport(tmp_path / "ledger.json", inner=httpx.MockTransport(pending), tokenize_payload=True)
    async with httpx.AsyncClient(transport=transport) as client:
        operation = asyncio.create_task(client.post(URL, json=payload()))
        await asyncio.wait_for(started.wait(), 1)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
    assert not paid
    assert transport.summary()["attempts"] == 0
    await transport.close()


@pytest.mark.asyncio
async def test_mixed_pricing_preserves_legacy_success_errors_and_remaining_budget_on_reload(tmp_path):
    ledger = tmp_path / "ledger.json"
    old_attempts = [
        {"id": 1, "model": "yandexgpt-5.1/latest", "status": "actual", "reserved_rub": "23.2",
         "prompt_tokens_upper_bound": 28000, "completion_tokens_upper_bound": 1000,
         "usage": {"prompt_tokens": 27000, "completion_tokens": 500, "total_tokens": 27500},
         "actual_cost_rub": "22.0"},
        {"id": 2, "model": "yandexgpt-5.1/latest", "status": "unknown", "reserved_rub": "0.6992",
         "prompt_tokens_upper_bound": 824, "completion_tokens_upper_bound": 50,
         "http_status": 503, "reason": "http_error"},
    ]
    ledger.write_text(json.dumps({"version": 1, "budget_rub": "25", "rate_rub_per_1000_tokens": "0.8",
                                 "attempts": old_attempts, "blocked": False}))

    def dispatch(request):
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1000, "completion_tokens": 400,
                                                  "total_tokens": 1400}})

    transport = BudgetTransport(ledger, inner=httpx.MockTransport(dispatch))
    assert transport.summary()["accounted_rub"] == pytest.approx(22.6992)
    assert transport.summary()["remaining_rub"] == pytest.approx(2.3008)
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post(URL, json=payload(model="gpt://test-folder/aliceai-llm-flash", max_tokens=500))
    state = transport.summary()
    assert state["accounted_rub"] == pytest.approx(22.6992 + 0.18)
    assert state["remaining_rub"] == pytest.approx(2.1208)
    saved = json.loads(ledger.read_text())
    assert saved["attempts"][:2] == old_attempts
    assert saved["attempts"][2]["input_rate_rub_per_1000_tokens"] == "0.1000"
    assert saved["attempts"][2]["output_rate_rub_per_1000_tokens"] == "0.2000"
    await transport.close()
    reloaded = BudgetTransport(ledger, inner=httpx.MockTransport(dispatch))
    assert reloaded.summary() == state
    await reloaded.close()


@pytest.mark.asyncio
async def test_flash_request_still_cannot_exceed_the_shared_budget(tmp_path):
    calls = []
    transport = BudgetTransport(tmp_path / "ledger.json", budget_rub=0.01,
                                inner=httpx.MockTransport(lambda request: calls.append(request) or success()))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderError) as error:
            await client.post(URL, json=payload(model="gpt://test-folder/aliceai-llm-flash"))
    assert error.value.code == "eval_budget_exceeded"
    assert not calls
    await transport.close()


@pytest.mark.asyncio
async def test_one_client_cannot_close_a_pool_used_by_another_active_request(monkeypatch, tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    pools = []

    class Pool(httpx.AsyncBaseTransport):
        def __init__(self, **kwargs):
            self.closed = False
            pools.append(self)

        async def handle_async_request(self, request):
            if json.loads(request.content)["messages"][0]["content"] == "slow":
                started.set()
                await release.wait()
            assert not self.closed, "Another client closed an active request's pool"
            return success()

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", Pool)
    transport = BudgetTransport(tmp_path / "ledger.json")
    async with httpx.AsyncClient(transport=transport) as slow_client:
        slow = asyncio.create_task(slow_client.post(URL, json=payload(messages=[{"role": "user", "content": "slow"}])))
        await asyncio.wait_for(started.wait(), 1)
        async with httpx.AsyncClient(transport=transport) as fast_client:
            await fast_client.post(URL, json=payload())
        assert len(pools) == 1 and not pools[0].closed
        release.set()
        await slow
    assert pools[0].closed
    assert transport.summary()["attempts"] == 2
    await transport.close()
