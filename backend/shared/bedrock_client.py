"""Claude on Amazon Bedrock for JSON-only AI steps (SPEC §14).

Rules enforced here for every call:
- Responses are validated against a pydantic model (object root).
- One retry with a stricter "return valid JSON" reminder.
- Never raises: on failure the result has `value=None` and the step degrades.
  AI failure never blocks a job.
- Prompts and responses are never logged; only purpose, counts, and latency.
"""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from shared.observability import logger, metrics

M = TypeVar("M", bound=BaseModel)

DEFAULT_MODEL_ID = "anthropic.claude-opus-5"
JSON_REMINDER = (
    "Your previous reply was not valid JSON for the required schema. "
    "Reply again with only a JSON object that matches the schema exactly."
)


class Outcome(StrEnum):
    OK = "ok"
    OK_AFTER_RETRY = "ok_after_retry"
    PARSE_FAILED = "parse_failed"
    ERROR = "error"


@dataclass(frozen=True)
class RawResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class LLMResult(Generic[M]):
    value: M | None
    outcome: Outcome
    purpose: str
    model_id: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    latency_ms: int

    def audit_details(self, row_count: int) -> dict[str, Any]:
        """Details for the AI_INVOCATION audit event. No prompt or response content."""
        return {
            "purpose": self.purpose,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "row_count": row_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "outcome": str(self.outcome),
        }


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


class BedrockClient(ABC):
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    @abstractmethod
    def _invoke_raw(self, system: str, prompt: str, schema: dict[str, Any]) -> RawResponse: ...

    def invoke_json(
        self,
        *,
        purpose: str,
        prompt_version: str,
        system: str,
        prompt: str,
        output_model: type[M],
    ) -> LLMResult[M]:
        schema = output_model.model_json_schema()
        started = time.monotonic()
        tokens_in = tokens_out = 0
        value: M | None = None
        outcome = Outcome.PARSE_FAILED

        for attempt in range(2):
            user_prompt = prompt if attempt == 0 else f"{prompt}\n\n{JSON_REMINDER}"
            try:
                raw = self._invoke_raw(system, user_prompt, schema)
            except Exception as exc:
                logger.warning(
                    "bedrock call failed",
                    extra={"purpose": purpose, "error_type": type(exc).__name__},
                )
                outcome = Outcome.ERROR
                break
            tokens_in += raw.input_tokens
            tokens_out += raw.output_tokens
            try:
                value = output_model.model_validate_json(_strip_fences(raw.text))
            except (ValidationError, ValueError):
                continue
            outcome = Outcome.OK if attempt == 0 else Outcome.OK_AFTER_RETRY
            break

        latency_ms = int((time.monotonic() - started) * 1000)
        metrics.add_metric(name="AIInvocations", unit="Count", value=1)
        metrics.add_metric(name="AIInputTokens", unit="Count", value=tokens_in)
        metrics.add_metric(name="AIOutputTokens", unit="Count", value=tokens_out)
        if outcome in (Outcome.PARSE_FAILED, Outcome.ERROR):
            metrics.add_metric(name="AIParseFailures", unit="Count", value=1)
        logger.info(
            "bedrock call finished",
            extra={"purpose": purpose, "outcome": str(outcome), "latency_ms": latency_ms},
        )
        return LLMResult(
            value=value,
            outcome=outcome,
            purpose=purpose,
            model_id=self.model_id,
            prompt_version=prompt_version,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            latency_ms=latency_ms,
        )


class BedrockMantleClient(BedrockClient):
    """Real client: Anthropic SDK's Bedrock (Mantle) client with structured outputs."""

    def __init__(self, model_id: str | None = None, region: str | None = None) -> None:
        from anthropic import AnthropicBedrockMantle

        super().__init__(model_id or os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID))
        self._client = AnthropicBedrockMantle(
            aws_region=region or os.environ.get("AWS_REGION", "us-east-1")
        )

    def _invoke_raw(self, system: str, prompt: str, schema: dict[str, Any]) -> RawResponse:
        response = self._client.messages.create(
            model=self.model_id,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": schema},
            },
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("model declined the request")
        text = next((b.text for b in response.content if b.type == "text"), "")
        return RawResponse(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


@dataclass
class FakeBedrockClient(BedrockClient):
    """Scripted responses for tests and the mock-only deployment.

    Each queued item is a JSON string, a pydantic model/dict (serialized), or an
    Exception (raised). An empty queue returns `default`.
    """

    responses: deque[Any] = field(default_factory=deque)
    model_id: str = "fake-model"
    default: Any = "{}"
    prompts: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(self.model_id)

    def queue(self, *items: Any) -> FakeBedrockClient:
        self.responses.extend(items)
        return self

    def _invoke_raw(self, system: str, prompt: str, schema: dict[str, Any]) -> RawResponse:
        self.prompts.append(prompt)
        item = self.responses.popleft() if self.responses else self.default
        if isinstance(item, Exception):
            raise item
        if isinstance(item, BaseModel):
            item = item.model_dump_json()
        elif isinstance(item, dict | list):
            item = json.dumps(item)
        return RawResponse(text=str(item), input_tokens=100, output_tokens=20)
