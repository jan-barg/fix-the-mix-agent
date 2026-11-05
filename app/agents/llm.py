"""Every model call goes through here (DESIGN.md §7.1).

Agent code names a role ("lead", "researcher", ...), never a provider. The role's model and effort
come from app.config.ROLES, so switching a role between Claude and Gemini is an env var.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

import litellm
from pydantic import BaseModel, ValidationError

from app.config import ROLES

log = logging.getLogger(__name__)
litellm.suppress_debug_info = True

T = TypeVar("T", bound=BaseModel)

# Finish reasons that mean the model ended normally (LiteLLM maps provider reasons onto these).
NORMAL_FINISH = {"stop", "tool_calls"}


class ModelCallError(Exception):
    """A call that returned but cannot be used: cut off, filtered, or not matching its schema."""


@dataclass
class Completion:
    message: dict[str, Any]  # assistant message as returned; append it to the history unchanged
    text: str
    tool_calls: list[Any]
    finish_reason: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    seconds: float
    extra: dict[str, Any] = field(default_factory=dict)


async def complete(
    role: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    response_format: type[BaseModel] | None = None,
) -> Completion:
    """One model call for a role. LiteLLM retries rate limits and server errors."""
    config = ROLES[role]
    kwargs: dict[str, Any] = {}
    if config.effort:
        kwargs["reasoning_effort"] = config.effort
    if tools:
        kwargs["tools"] = tools
    if response_format:
        kwargs["response_format"] = response_format

    start = time.perf_counter()
    response = await litellm.acompletion(model=config.model, messages=messages, num_retries=2, **kwargs)
    seconds = time.perf_counter() - start

    choice = response.choices[0]
    # model_dump() keeps it a plain dict and keeps Claude's thinking_blocks, which must be
    # sent back unchanged on the next turn.
    message = choice.message.model_dump()
    try:
        cost = litellm.completion_cost(response)
    except Exception:
        cost = None
    usage = response.usage
    result = Completion(
        message=message,
        text=choice.message.content or "",
        tool_calls=list(choice.message.tool_calls or []),
        finish_reason=choice.finish_reason or "",
        model=config.model,
        input_tokens=getattr(usage, "prompt_tokens", 0),
        output_tokens=getattr(usage, "completion_tokens", 0),
        cost_usd=cost,
        seconds=seconds,
    )
    log.info("%s call to %s: %d in / %d out tokens, $%s, %.1fs, finish=%s", role, config.model,
             result.input_tokens, result.output_tokens, f"{cost:.4f}" if cost is not None else "?",
             seconds, result.finish_reason)

    if result.finish_reason not in NORMAL_FINISH:
        raise ModelCallError(f"{role} call ended with finish_reason={result.finish_reason!r}")
    return result


async def complete_json(role: str, messages: list[dict[str, Any]], schema: type[T]) -> tuple[T, Completion]:
    """Ask for JSON matching schema and validate it. One retry that shows the model its error."""
    for attempt in range(2):
        result = await complete(role, messages, response_format=schema)
        try:
            return schema.model_validate_json(result.text), result
        except ValidationError as e:
            if attempt == 1:
                raise ModelCallError(f"{role} reply did not match {schema.__name__}: {e}") from e
            messages = [
                *messages,
                result.message,
                {"role": "user", "content": f"That reply did not match the required schema:\n{e}\n"
                                            "Reply again with only the corrected JSON."},
            ]
    raise AssertionError("unreachable")
