"""Orchestration: cache -> facts -> Jev probes -> composition. Fails open on model trouble."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Protocol

from . import decide
from .config import Thresholds, ToolMap
from .facts import extract_facts
from .judge import Judge
from .resilience import SingleFlightCache
from .signals import ChoiceSig, NoulSig

log = logging.getLogger("incident_eval")
RULESET_VERSION = "incident-ai/2.0"  # bump when questions, thresholds, or composition change


class Metrics(Protocol):
    def evaluation(self, status: str, priority: str) -> None: ...
    def reasons(self, reasons: list[str]) -> None: ...
    def questions(self, n: int) -> None: ...


class NullMetrics:
    def evaluation(self, status: str, priority: str) -> None: ...
    def reasons(self, reasons: list[str]) -> None: ...
    def questions(self, n: int) -> None: ...


def trace_key(trace: dict[str, Any]) -> str:
    blob = json.dumps({"v": RULESET_VERSION, "t": trace}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _view(signals: dict) -> dict:
    """JSON-friendly copy of what Jev said, for audit and for building a labeled calibration set."""
    return {g: {q: (round(s.p, 3) if isinstance(s, NoulSig)
                    else {"choice": s.choice, "confidence": round(s.confidence, 3)})
                for q, s in qs.items()} for g, qs in signals.items()}


class EvaluationService:
    def __init__(self, *, judge: Judge, thresholds: Thresholds, tools: ToolMap, cache: SingleFlightCache,
                 responder_suffixes: tuple = ("-oncall",), metrics: Metrics | None = None) -> None:
        self._judge, self._th, self._tools = judge, thresholds, tools
        self._cache, self._suffixes = cache, responder_suffixes
        self._m: Metrics = metrics or NullMetrics()

    async def evaluate(self, trace: dict[str, Any]) -> dict[str, Any]:
        key = trace_key(trace)

        async def compute() -> tuple[dict[str, Any], bool]:
            verdict = await self._evaluate(trace, key)
            return verdict, verdict["status"] == "ok"  # never cache partial/degraded verdicts

        verdict, cached = await self._cache.get_or_compute(key, compute)
        return {**verdict, "cached": cached}

    async def evaluate_many(self, traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
        import asyncio
        results = await asyncio.gather(*(self.evaluate(t) for t in traces), return_exceptions=True)
        return [r if not isinstance(r, BaseException)
                else {"status": "error", "session_id": t.get("session_id"), "error": type(r).__name__}
                for t, r in zip(traces, results)]

    async def _evaluate(self, trace: dict[str, Any], key: str) -> dict[str, Any]:
        t0 = time.perf_counter()
        facts = extract_facts(trace, self._tools, self._suffixes)       # code
        judged = await self._judge.run(facts, trace)                    # Jev (fan-out)
        out = decide.compose(facts, judged.signals, self._th, self._tools, judged.status)  # code

        self._m.evaluation(judged.status, out["decision"]["priority"])
        self._m.reasons(out["decision"]["reasons"])
        self._m.questions(judged.asked)
        log.info(json.dumps({"event": "evaluation", "session_id": facts.session_id,
                             "status": judged.status, "priority": out["decision"]["priority"],
                             "reasons": out["decision"]["reasons"], "requests": judged.requests,
                             "questions": judged.asked, "failed": judged.failed,
                             "ms": round((time.perf_counter() - t0) * 1000)}))
        return {
            "evaluation_id": "ev_" + key[:16],
            "session_id": facts.session_id,
            "status": judged.status,                 # ok | partial | degraded
            "ruleset_version": RULESET_VERSION,
            "model": judged.model,
            **out,
            "signals": _view(judged.signals),
            "jev": {"requests": judged.requests, "questions": judged.asked,
                    "failed": judged.failed, "unanswered": sorted(judged.unanswered),
                    "request_ids": judged.request_ids,
                    "input_tokens": judged.input_tokens, "output_tokens": judged.output_tokens},
        }
