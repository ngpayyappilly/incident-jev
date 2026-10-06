"""Step 3: run the probes through Jev. Stage 1 fans out in parallel; stage 2 asks dependent questions."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .config import Thresholds
from .facts import Facts
from .jev_gateway import JevError, JevGateway
from .probes import build_stage1, build_stage2, pack
from .signals import Signals, absorb


@dataclass
class JudgeResult:
    signals: Signals = field(default_factory=dict)
    requests: int = 0
    failed: list = field(default_factory=list)       # reasons, one per failed request
    unanswered: set = field(default_factory=set)     # question names Jev never answered
    model: str | None = None
    request_ids: list = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    asked: int = 0

    @property
    def status(self) -> str:
        if self.asked == 0:
            return "ok"
        answered = self.asked - len(self.unanswered)
        if not self.unanswered:
            return "ok"
        return "degraded" if answered == 0 else "partial"


class Judge:
    def __init__(self, gateway: JevGateway, thresholds: Thresholds, max_questions: int) -> None:
        self._gw, self._th, self._max_q = gateway, thresholds, max_questions

    async def run(self, facts: Facts, trace: dict[str, Any]) -> JudgeResult:
        result = JudgeResult()
        await self._run_batches(pack(build_stage1(facts), self._max_q), result)
        stage2 = build_stage2(facts, trace, result.signals, self._th)
        if stage2:
            await self._run_batches(pack(stage2, self._max_q), result)
        return result

    async def _run_batches(self, batches: list, result: JudgeResult) -> None:
        outcomes = await asyncio.gather(*(self._gw.ask(s, q) for s, q in batches), return_exceptions=True)
        for (_, questions), out in zip(batches, outcomes):
            result.requests += 1
            result.asked += len(questions)
            if isinstance(out, JevError):
                result.failed.append(out.reason)
                result.unanswered |= set(questions)
                continue
            if isinstance(out, BaseException):
                raise out  # a bug, not a model failure: surface it
            answered = absorb(result.signals, out)
            result.unanswered |= set(questions) - answered
            result.model = getattr(out, "model", result.model)
            rid = getattr(out, "request_id", None)
            if rid:
                result.request_ids.append(rid)
            usage = getattr(out, "usage", None)
            result.input_tokens += getattr(usage, "input_tokens", None) or 0
            result.output_tokens += getattr(usage, "output_tokens", None) or 0
