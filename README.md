# ParcelPilot AI Support

An AI support system for ParcelPilot, a B2B logistics platform. It answers
questions from the supplied policy documents and operational data, and it can
draft escalations, ticket updates, follow-ups and service credits for a human to
confirm.

It runs two user contexts against the same data: a customer-facing assistant
scoped to one account, and an internal one for support staff and managers.

Live demo: _<add URL after deploying>_

## Running it

Python 3.9 or newer.

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then add one API key
cd .. && ./start.sh
```

Open http://localhost:8000. The persona switcher is at the bottom left, four
customers, two agents, one manager.

`start.sh` extracts the PDFs and rebuilds the search index before starting, so
the index can never disagree with the source documents.

### API key

Any provider that speaks the OpenAI chat-completions shape works. Set two
variables in `backend/.env`:

```
LLM_PROVIDER=gemini
LLM_MODEL=gemini-3.1-flash-lite
GEMINI_API_KEY=...
```

Groq works the same way (`LLM_PROVIDER=groq`, `LLM_MODEL=openai/gpt-oss-20b`).
Both have free tiers.

The free-tier limit that bites is requests per minute, not tokens, because each
tool call is its own request. Read the real numbers from your own AI Studio Rate
Limit page rather than any published summary, mine were 5/min on 2.5 Flash and
15/min on 3.1 Flash Lite, which is why that is the default.

Gemini 3 models attach an opaque `thought_signature` to each function call and
reject the next request if it is not echoed back. The client handles this by
replaying the provider's own assistant message verbatim instead of rebuilding it
(`app/agent/llm.py`), so any provider's opaque fields survive a replay.

### Tests

```bash
cd backend && python -m pytest tests -q
```

88 tests, half a second, no API key needed. They pin every fee, credit and
response target in the dataset, plus the five traps planted in the data pack.

## Reference time

The dataset is a snapshot. Every time-based answer is measured from
**Sunday 16 August 2026, 11:00 IST**, taken from the workbook's README sheet, not
from the real clock. Sunday matters: LumenWorks' agreement excludes weekend
support, so their response clocks have not started.

## Layout

```
data/                 the supplied PDFs and workbook, the only information base
backend/app/
  corpus/             PDF extraction and BM25 retrieval
  sources/            numeric rules as JSON, each carrying its source quote
  domain/             the decision engines: cancellation, credit, SLA, insights
  security/           roles, principals, and the scoped data layer
  actions/            prepared actions and the confirmation gate
  agent/              tools, prompts, and the agent loop
  main.py             HTTP API and SSE
backend/static/       single-page frontend
backend/tests/        ground truth, traps, retrieval, loop mechanics
```

Notes on the design are in [ARCHITECTURE.md](ARCHITECTURE.md),
[PRODUCT.md](PRODUCT.md) and [AI_USAGE.md](AI_USAGE.md).

## Deploying

`render.yaml` and a `Dockerfile` are included. Both run `start.sh`, which honours
`$PORT`. Set the API key as a secret in the host; nothing else is required.

Prepared actions are held in memory, so they reset when the process restarts.
That is deliberate, the supplied dataset is read-only and is never mutated, and
the assignment allows the action tool to be mocked locally.
