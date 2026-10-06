# Agent instructions

## Project shape

- Read [README.md](README.md) first for setup, the evaluation flow, and production adaptation points.
- Keep the pipeline boundary intact: `facts.py` extracts deterministic facts, `probes.py` builds typed questions, `judge.py` orchestrates Jev calls, and `decide.py` composes the deterministic verdict.
- Treat `app/facts.py`, `app/probes.py`, and `app/decide.py` as pure, SDK-independent logic. Keep network calls, retries, concurrency, circuit breaking, and SDK exception handling in `app/jev_gateway.py`, `app/judge.py`, and `app/resilience.py`.
- `app/service.py` owns the top-level evaluation flow, caching, and metrics; `app/main.py` owns the FastAPI lifecycle and HTTP endpoints.

## Development workflow

- Install dependencies with `python -m pip install -r requirements.txt`.
- Run the full test suite with `python -m unittest discover -s tests -t .`.
- Tests use stdlib `unittest`, including `unittest.IsolatedAsyncioTestCase`; use `tests/fake_jev.py` rather than live Jev calls.
- Use `tests/fixtures/trace.json` as the representative trace schema and add focused tests beside the affected module.
- Run the service with the environment variables and `uvicorn` command documented in [README.md](README.md). `TYPESAFE_API_KEY` is required by `Settings.from_env()`; keep `JEV_MODEL` pinned in production.

## Conventions and invariants

- Preserve `from __future__ import annotations`, frozen dataclasses, async boundaries, and the `{group}__{question}` naming convention (`signals.SEP`).
- Preserve two-stage questioning: stage 1 runs base questions in parallel; stage 2 asks only conditional follow-ups supported by stage 1 answers.
- Keep degraded-mode behavior intact: rule-based findings in `decide.py` must remain useful when Jev is unavailable, and degraded verdicts must not be cached as successful results.
- Classify Jev failures by exception class name through `jev_gateway.classify()` without importing SDK exception classes into decision logic.
- When changing thresholds or tool names, update `Thresholds`/`ToolMap` in [app/config.py](app/config.py) and the related fixture/tests. Tool names, responder suffixes, question wording, and decision weights are deployment-specific.
- The single-flight cache and circuit breaker are in-process. Do not assume they coordinate across replicas; use a shared store or coordination mechanism before adding multi-process guarantees.

## Change discipline

- Prefer the smallest change at the owning module and avoid unrelated refactors.
- Keep pure logic free of side effects and external SDK imports.
- Extend the fake Jev and focused tests when adding or changing question types, signals, failure modes, or endpoint behavior.
- Validate behavior with the narrowest relevant unittest first, then run the full discovery command.