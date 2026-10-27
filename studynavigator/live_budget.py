"""Conservative, persistent accounting for explicitly budgeted Yandex requests.

This application/evaluation guard is not a substitute for the provider's bill.
Every HTTP attempt is reserved before dispatch.  The byte-based input bound
deliberately includes the whole JSON request and extra framing allowance; it is
not advertised as an exact tokenizer.  Missing usage never becomes zero cost.
"""

from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import httpx

from .providers import ProviderError


TOKEN_RATE_RUB = Decimal("0.0008")
PRICE_PROFILES = {"yandexgpt-5.1": (TOKEN_RATE_RUB, TOKEN_RATE_RUB),
                  "aliceai-llm-flash": (Decimal("0.0001"), Decimal("0.0002"))}
# Server ceiling only; the CLI diagnostic runner separately enforces 25 RUB.
MAX_BUDGET_RUB = Decimal("500")
MODEL = re.compile(r"^gpt://[A-Za-z0-9_-]{1,100}/((?:yandexgpt-5\.1|aliceai-llm-flash)(?:/latest)?)$")
ALLOWED_PAYLOAD_FIELDS = {
    "model", "messages", "tools", "tool_choice", "max_tokens", "response_format",
    "temperature", "top_p", "parallel_tool_calls", "stream", "n",
}


def _failure(code="eval_budget_exceeded"):
    message = ("Достигнут лимит расходов API сервиса. Новые запросы не отправлены."
               if code == "eval_budget_exceeded" else
               "Не удалось безопасно проверить бюджет live-запроса. Запрос не отправлен.")
    return ProviderError(code, message)


def _money(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise _failure("eval_budget_invalid") from exc
    if not result.is_finite() or result < 0:
        raise _failure("eval_budget_invalid")
    return result


def _rates(model):
    profile = model.removesuffix("/latest")
    if profile not in PRICE_PROFILES:
        raise _failure("eval_budget_invalid")
    return PRICE_PROFILES[profile]


class BudgetTransport(httpx.AsyncBaseTransport):
    """Wrap each paid attempt; share one ledger across sequential evaluation runs.

    Providers create short-lived AsyncClients. ``aclose`` closes the currently
    owned connection pool but leaves this wrapper reusable for the next client.
    The runner calls ``await transport.close()`` once to end the evaluation.
    """

    def __init__(self, ledger_path, budget_rub=25, inner=None, *, tokenize_payload=False):
        self.ledger_path = Path(ledger_path)
        self.budget = _money(budget_rub)
        if not 0 < self.budget <= MAX_BUDGET_RUB:
            raise _failure("eval_budget_invalid")
        self.inner = inner
        self._owns_inner = inner is None
        if type(tokenize_payload) is not bool:
            raise _failure("eval_budget_invalid")
        self.tokenize_payload = tokenize_payload
        self._closed = False
        self._active_requests = 0
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            ledger = self._load()
            self._save(ledger)

    @contextlib.contextmanager
    def _locked(self):
        lock_path = self.ledger_path.with_name(self.ledger_path.name + ".lock")
        with lock_path.open("a+b") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _load(self):
        if not self.ledger_path.exists():
            return {"version": 1, "budget_rub": str(self.budget),
                    "rate_rub_per_1000_tokens": "0.8", "attempts": [], "blocked": False}
        try:
            ledger = json.loads(self.ledger_path.read_text())
            if (ledger.get("version") != 1 or _money(ledger.get("budget_rub")) != self.budget
                    or ledger.get("rate_rub_per_1000_tokens") != "0.8"
                    or not isinstance(ledger.get("attempts"), list)
                    or type(ledger.get("blocked")) is not bool):
                raise ValueError("Invalid ledger metadata")
            for index, attempt in enumerate(ledger["attempts"], 1):
                if (attempt.get("id") != index or attempt.get("status") not in {"pending", "unknown", "actual"}
                        or attempt.get("model") not in {"yandexgpt-5.1", "yandexgpt-5.1/latest",
                                                       "aliceai-llm-flash", "aliceai-llm-flash/latest"}):
                    raise ValueError("Invalid ledger attempt")
                input_rate, output_rate = _rates(attempt["model"])
                # Version 1 rows without per-attempt rates are original Pro 5.1
                # entries. Keep all their recorded costs and reservations intact.
                for name, rate in (("input_rate_rub_per_1000_tokens", input_rate),
                                   ("output_rate_rub_per_1000_tokens", output_rate)):
                    if name in attempt and _money(attempt[name]) != rate * 1000:
                        raise ValueError("Invalid ledger pricing")
                    if name not in attempt and not attempt["model"].startswith("yandexgpt-5.1"):
                        raise ValueError("Missing ledger pricing")
                _money(attempt["reserved_rub"])
                if attempt["status"] == "actual":
                    _money(attempt["actual_cost_rub"])
            return ledger
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise _failure("eval_budget_invalid") from exc

    def _save(self, ledger):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.ledger_path.parent,
                                             prefix=self.ledger_path.name + ".", delete=False) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                json.dump(ledger, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.ledger_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _totals(ledger):
        actual = sum((_money(item["actual_cost_rub"]) for item in ledger["attempts"]
                      if item["status"] == "actual"), Decimal(0))
        unknown = sum((_money(item["reserved_rub"]) for item in ledger["attempts"]
                       if item["status"] != "actual"), Decimal(0))
        return actual, unknown

    def summary(self):
        with self._locked():
            ledger = self._load()
            actual, unknown = self._totals(ledger)
            accounted = actual + unknown
            return {"budget_rub": float(self.budget), "accounted_rub": float(accounted),
                    "actual_cost_rub": float(actual), "unknown_reserved_rub": float(unknown),
                    "remaining_rub": float(max(Decimal(0), self.budget - accounted)),
                    "attempts": len(ledger["attempts"]), "blocked": ledger["blocked"],
                    "blocked_reason": ledger.get("blocked_reason")}

    @staticmethod
    def _validate_payload(payload):
        if not isinstance(payload, dict) or set(payload) - ALLOWED_PAYLOAD_FIELDS:
            raise _failure("eval_budget_invalid")
        match = MODEL.fullmatch(payload.get("model", "")) if isinstance(payload.get("model"), str) else None
        messages, tools = payload.get("messages"), payload.get("tools", [])
        maximum = payload.get("max_tokens")
        if (match is None or not isinstance(messages, list) or not 1 <= len(messages) <= 30
                or not all(isinstance(message, dict) for message in messages)
                or not isinstance(tools, list) or len(tools) > 3
                or not all(isinstance(tool, dict) for tool in tools)
                or type(maximum) is not int or not 1 <= maximum <= 32_768
                or payload.get("stream", False) is not False
                or type(payload.get("n", 1)) is not int or payload.get("n", 1) != 1):
            raise _failure("eval_budget_invalid")
        return match.group(1), maximum

    async def _input_bound(self, request, payload, body):
        byte_bound = len(body) + 2048
        estimate = {"estimate_strategy": "utf8_bytes", "payload_sha256": hashlib.sha256(body).hexdigest()}
        if not self.tokenize_payload:
            return byte_bound, estimate
        try:
            literal_payload = body.decode("utf-8")
        except UnicodeDecodeError:
            estimate["tokenizer_status"] = "unavailable_or_invalid"
            return byte_bound, estimate
        # Tools are intentionally serialized as literal user text here. Native
        # tokenizeCompletion ignores its tools field, so do not use that field.
        native_payload = {"modelUri": payload["model"],
                          "completionOptions": {"stream": False, "maxTokens": str(payload["max_tokens"])},
                          "messages": [{"role": "user", "text": literal_payload}]}
        native_headers = {"Accept": "application/json"}
        for name in ("Authorization", "OpenAI-Project"):
            if name in request.headers:
                native_headers[name] = request.headers[name]
        native_request = httpx.Request(
            "POST", "https://ai.api.cloud.yandex.net/foundationModels/v1/tokenizeCompletion",
            headers=native_headers, json=native_payload,
            extensions={"timeout": {"connect": 10, "read": 10, "write": 10, "pool": 10}},
        )
        try:
            async with asyncio.timeout(10):
                if self.inner is None:
                    self.inner = httpx.AsyncHTTPTransport(retries=0)
                response = await self.inner.handle_async_request(native_request)
                native_body = await response.aread()
            if response.status_code != 200 or len(native_body) > 4_000_000:
                raise ValueError("Invalid tokenizer response")
            result = json.loads(native_body)
            tokens, version = result.get("tokens"), result.get("modelVersion")
            if (not isinstance(tokens, list) or not 1 <= len(tokens) <= 32_768
                    or not isinstance(version, str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}", version) is None
                    or ("modelUri" in result and result["modelUri"] != payload["model"])):
                raise ValueError("Invalid tokenizer result")
            for token in tokens:
                if not isinstance(token, dict) or not isinstance(token.get("text"), str):
                    raise ValueError("Invalid tokenizer token")
                identifier = token.get("id")
                if not ((type(identifier) is int and identifier >= 0)
                        or (isinstance(identifier, str) and re.fullmatch(r"[0-9]+", identifier))):
                    raise ValueError("Invalid tokenizer ID")
                if type(token.get("special", False)) is not bool:
                    raise ValueError("Invalid tokenizer flag")
            native_bound = len(tokens) + 1024
            if native_bound + payload["max_tokens"] > 32_768:
                raise ValueError("Tokenizer exceeds context allowance")
            estimate.update(tokenizer_status="valid", native_input_tokens=len(tokens),
                            tokenizer_model_version=version)
            if native_bound < byte_bound:
                estimate["estimate_strategy"] = "literal_payload_tokenizer_plus_1024"
            return min(byte_bound, native_bound), estimate
        except (TimeoutError, httpx.HTTPError, ValueError, TypeError, AttributeError):
            # A failed free request cannot reduce the paid reservation. External
            # cancellation intentionally propagates without dispatching a model.
            estimate["tokenizer_status"] = "unavailable_or_invalid"
            return byte_bound, estimate

    def _reserve(self, payload, body_size, input_bound=None, estimate=None):
        model, maximum = self._validate_payload(payload)
        input_bound = body_size + 2048 if input_bound is None else input_bound
        input_rate, output_rate = _rates(model)
        reserved = input_bound * input_rate + maximum * output_rate
        with self._locked():
            ledger = self._load()
            actual, unknown = self._totals(ledger)
            if ledger["blocked"] or actual + unknown + reserved > self.budget:
                ledger["blocked"] = True
                ledger["blocked_reason"] = ledger.get("blocked_reason") or "insufficient_reservation"
                ledger["last_blocked_request"] = {"model": model, "prompt_tokens_upper_bound": input_bound,
                                                  "completion_tokens_upper_bound": maximum,
                                                  "reserved_rub": str(reserved), **(estimate or {})}
                self._save(ledger)
                raise _failure()
            attempt = {"id": len(ledger["attempts"]) + 1, "model": model,
                       "status": "pending", "prompt_tokens_upper_bound": input_bound,
                       "completion_tokens_upper_bound": maximum, "reserved_rub": str(reserved),
                       "input_rate_rub_per_1000_tokens": str(input_rate * 1000),
                       "output_rate_rub_per_1000_tokens": str(output_rate * 1000),
                       **(estimate or {})}
            ledger["attempts"].append(attempt)
            self._save(ledger)
            return attempt["id"]

    def _unknown(self, attempt_id, reason):
        with self._locked():
            ledger = self._load()
            attempt = ledger["attempts"][attempt_id - 1]
            attempt["status"] = "unknown"
            attempt["reason"] = reason
            self._save(ledger)

    def _settle(self, attempt_id, status_code, body):
        usage = None
        if 200 <= status_code < 300:
            try:
                data = json.loads(body)
                candidate = data.get("usage")
                if (isinstance(candidate, dict)
                        and all(type(candidate.get(name)) is int and candidate[name] >= 0
                                for name in ("prompt_tokens", "completion_tokens", "total_tokens"))
                        and candidate["prompt_tokens"] > 0
                        and candidate["total_tokens"] == candidate["prompt_tokens"] + candidate["completion_tokens"]):
                    usage = {name: candidate[name] for name in
                             ("prompt_tokens", "completion_tokens", "total_tokens")}
            except (ValueError, TypeError, AttributeError):
                pass
        violation = False
        with self._locked():
            ledger = self._load()
            attempt = ledger["attempts"][attempt_id - 1]
            attempt["http_status"] = status_code
            if usage is None:
                attempt["status"] = "unknown"
                attempt["reason"] = "missing_or_invalid_usage" if status_code < 400 else "http_error"
            else:
                input_rate, output_rate = _rates(attempt["model"])
                attempt.update(status="actual", usage=usage,
                               actual_cost_rub=str(usage["prompt_tokens"] * input_rate
                                                   + usage["completion_tokens"] * output_rate))
                violation = (usage["prompt_tokens"] > attempt["prompt_tokens_upper_bound"]
                             or usage["completion_tokens"] > attempt["completion_tokens_upper_bound"])
                actual, unknown = self._totals(ledger)
                if violation or actual + unknown > self.budget:
                    ledger["blocked"] = True
                    ledger["blocked_reason"] = "usage_exceeded_reservation"
                    violation = True
            self._save(ledger)
        if violation:
            raise _failure()

    async def handle_async_request(self, request):
        self._active_requests += 1
        try:
            return await self._handle_async_request(request)
        finally:
            self._active_requests -= 1

    async def _handle_async_request(self, request):
        if (self._closed or request.method != "POST" or request.url.scheme != "https"
                or request.url.host != "ai.api.cloud.yandex.net"
                or request.url.path != "/v1/chat/completions" or request.url.query):
            raise _failure("eval_budget_invalid")
        try:
            body = await request.aread()
            if len(body) > 1_000_000:
                raise ValueError("Oversized request")
            payload = json.loads(body)
        except (ValueError, TypeError):
            raise _failure("eval_budget_invalid") from None
        self._validate_payload(payload)
        with self._locked():
            if self._load()["blocked"]:
                raise _failure()
        input_bound, estimate = await self._input_bound(request, payload, body)
        attempt_id = self._reserve(payload, len(body), input_bound, estimate)
        try:
            if self.inner is None:
                self.inner = httpx.AsyncHTTPTransport(retries=0)
            response = await self.inner.handle_async_request(request)
            response_body = await response.aread()
        except BaseException:
            self._unknown(attempt_id, "transport_or_read_error")
            raise
        self._settle(attempt_id, response.status_code, response_body)
        return response

    async def aclose(self):
        """Close a client-owned pool without discarding the shared ledger."""
        if self._owns_inner and self.inner is not None and self._active_requests == 0:
            inner, self.inner = self.inner, None
            await inner.aclose()

    async def close(self):
        if not self._closed:
            self._closed = True
            if self.inner is not None:
                inner, self.inner = self.inner, None
                await inner.aclose()
