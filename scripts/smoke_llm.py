"""Check that a model behaves through LiteLLM the way the agents need (DESIGN.md §7.1).

Run: uv run python scripts/smoke_llm.py [model ...]
Default model: anthropic/claude-opus-5-5. Each check prints PASS or FAIL with details.
"""

import asyncio
import base64
import io
import json
import sys

import litellm
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_band_energy",
            "description": "Get the measured energy of one frequency band for one track, in dB.",
            "parameters": {
                "type": "object",
                "properties": {
                    "track": {"type": "string", "enum": ["your_track", "reference_a"]},
                    "band": {"type": "string", "enum": ["sub", "bass"]},
                },
                "required": ["track", "band"],
                "additionalProperties": False,
            },
        },
    }
]
FAKE_ENERGY = {"your_track": -14.2, "reference_a": -11.8}


class Finding(BaseModel):
    observation: str
    difference_db: float
    confidence: str


def report(name: str, ok: bool, detail: str) -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")
    return ok


def cost(response) -> str:
    try:
        return f"${litellm.completion_cost(response):.4f}"
    except Exception:
        return "cost unknown"


async def check_tools_and_history(model: str) -> list[bool]:
    """Parallel tool calls, then two more turns on an append-only history.

    The first prompt asks for some planning at high effort so the model actually thinks; the
    later turns then prove that a history carrying thinking blocks is accepted.
    """
    results = []
    messages = [
        {"role": "system", "content": "You compare audio measurements. Use tools; never guess numbers."},
        {"role": "user", "content": "My techno drop feels weaker than reference_a in the low end, but only on "
                                    "club systems, not on headphones. Think about which of the two bands best "
                                    "explains that, then request that band for both tracks at once, in a "
                                    "single response."},
    ]
    first = await litellm.acompletion(model=model, messages=messages, tools=TOOLS, reasoning_effort="high")
    reply = first.choices[0].message
    calls = reply.tool_calls or []
    results.append(report("parallel tool calls", len(calls) >= 2,
                          f"{len(calls)} tool call(s) in one response, {cost(first)}"))

    dumped = reply.model_dump()
    thinking = dumped.get("thinking_blocks") or []
    print(f"        assistant message keeps {len(thinking)} thinking block(s)")
    messages.append(dumped)
    for call in calls:
        args = json.loads(call.function.arguments)
        value = FAKE_ENERGY.get(args.get("track"), -20.0)
        messages.append({"role": "tool", "tool_call_id": call.id,
                         "content": json.dumps({"band": args.get("band"), "energy_db": value})})
    if not calls:
        return results + [report("turn after tool results", False, "skipped: no tool calls")]

    try:
        second = await litellm.acompletion(model=model, messages=messages, tools=TOOLS, reasoning_effort="low")
        text = second.choices[0].message.content or ""
        results.append(report("turn after tool results", "2.4" in text or "2,4" in text,
                              f"answer mentions the 2.4 dB difference: {text[:120]!r}, {cost(second)}"))
        messages.append(second.choices[0].message.model_dump())
    except Exception as e:
        return results + [report("turn after tool results", False, f"{type(e).__name__}: {str(e)[:300]}")]

    messages.append({"role": "user", "content": "In one sentence: which track has more sub energy?"})
    try:
        third = await litellm.acompletion(model=model, messages=messages, tools=TOOLS, reasoning_effort="low")
        text = third.choices[0].message.content or ""
        results.append(report("follow-up turn on full history", "reference" in text.lower(),
                              f"{text[:120]!r}, {cost(third)}"))
    except Exception as e:
        results.append(report("follow-up turn on full history", False, f"{type(e).__name__}: {str(e)[:300]}"))
    return results


async def check_image(model: str) -> bool:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(4, 3), dpi=100)
    ax.plot([20, 40, 57, 80, 120], [-30, -18, -9, -16, -28])
    ax.set_title("Low-end peak at 57 Hz")
    ax.set_xlabel("Frequency (Hz)")
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    try:
        r = await litellm.acompletion(model=model, reasoning_effort="low", messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": "What frequency does the chart title name? Answer with the number only."},
        ]}])
        text = r.choices[0].message.content or ""
        return report("PNG image input", "57" in text, f"{text.strip()[:60]!r}, {cost(r)}")
    except Exception as e:
        return report("PNG image input", False, f"{type(e).__name__}: {str(e)[:300]}")


async def check_structured_output(model: str) -> bool:
    try:
        r = await litellm.acompletion(model=model, reasoning_effort="low", response_format=Finding, messages=[
            {"role": "user", "content": "Your track's sub band measures -14.2 dB and the reference's -11.8 dB. "
                                        "Report the finding."}])
        content = r.choices[0].message.content or ""
        finding = Finding.model_validate_json(content)
        return report("response_format (Pydantic schema)", abs(finding.difference_db) == 2.4,
                      f"{finding.model_dump()}, {cost(r)}")
    except Exception as e:
        return report("response_format (Pydantic schema)", False, f"{type(e).__name__}: {str(e)[:300]}")


async def check_effort(model: str) -> bool:
    try:
        for effort in ("low", "high"):
            r = await litellm.acompletion(model=model, reasoning_effort=effort,
                                          messages=[{"role": "user", "content": "Reply with the word ok."}])
            details = getattr(r.usage, "completion_tokens_details", None)
            reasoning = getattr(details, "reasoning_tokens", None) if details else None
            print(f"        effort={effort}: {r.usage.completion_tokens} output tokens "
                  f"({reasoning} reasoning), {cost(r)}")
        return report("reasoning_effort accepted", True, "low and high both ran")
    except Exception as e:
        return report("reasoning_effort accepted", False, f"{type(e).__name__}: {str(e)[:300]}")


async def main(models: list[str]) -> int:
    failures = 0
    for model in models:
        print(f"\n{model}")
        results = await check_tools_and_history(model)
        results += await asyncio.gather(check_image(model), check_structured_output(model), check_effort(model))
        failures += results.count(False)
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:] or ["anthropic/claude-opus-5-5"])))
