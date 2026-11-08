"""No-tool baselines for the README's sample queries (SPEC.md §12.3).

    PYTHONPATH=. uv run python scripts/baselines.py

The same model as the Lead, with no tools and no system prompt, in one conversation like the agent's:
the first message gives what the producer typed into the app (which track is which, their comments)
and sample 1; samples 2 and 3 follow. The model never gets the audio or any measurement. The raw
answers go to demo/samples/baseline.json.
"""

import asyncio

from app.agents import llm
from app.config import ROLES
from samples import QUERIES, now, save, setup


async def main() -> None:
    record = {"model": ROLES["baseline"].model, "tools": None, "system_prompt": None, "started_at": now(),
              "exchanges": []}
    messages: list[dict] = []
    for n, question in enumerate(QUERIES, 1):
        content = f"{setup()}\n\n{question}" if n == 1 else question
        messages.append({"role": "user", "content": content})
        reply = await llm.complete("baseline", messages)
        messages.append({"role": "assistant", "content": reply.text})
        record["exchanges"].append({"label": f"sample {n}", "message": content, "response": reply.text,
                                    "input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
                                    "cost_usd": reply.cost_usd, "seconds": round(reply.seconds)})
        print(f"\n=== sample {n} ({round(reply.seconds)} s, ${reply.cost_usd or 0:.3f})\n{reply.text}")
    record["ended_at"] = now()
    print(f"\nwritten to {save('baseline.json', record)}")


if __name__ == "__main__":
    asyncio.run(main())
