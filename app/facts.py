"""Step 1 of the workflow: turn a raw trace into structured facts. Pure code, no model.

Everything verifiable (which tools ran, what the SLI said, who was paged, whether an approval
token was passed) is extracted here, so Jev is only asked about what code cannot read:
the meaning of free text.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import ToolMap


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class Message:
    key: str            # stable state key: out_14 | resp_15 | stk_18 | final
    seq: int | None
    ts: datetime | None
    role: str           # agent | responder | stakeholder
    author: str
    text: str


@dataclass(frozen=True)
class Action:
    seq: int
    tool: str
    kind: str           # rollback | failover_or_traffic_shift
    has_approval: bool
    succeeded: bool


@dataclass(frozen=True)
class SliReading:
    seq: int
    ts: datetime | None
    error_rate_pct: float


@dataclass
class Facts:
    session_id: str
    alert_text: str | None = None
    first_alert_ts: datetime | None = None
    owner_team: str | None = None
    revenue_critical: bool | None = None
    declared_severity: str | None = None
    declared_seq: int | None = None
    declared_ts: datetime | None = None
    closed_seq: int | None = None
    closed_ts: datetime | None = None
    close_resolution: str | None = None
    paged: list = field(default_factory=list)            # [(seq, team)]
    actions: list = field(default_factory=list)          # [Action]
    sli: list = field(default_factory=list)              # [SliReading]
    messages: list = field(default_factory=list)         # [Message]
    policy: dict = field(default_factory=dict)
    succeeded_tools: dict = field(default_factory=dict)  # tool -> [seq]

    def last_sli_before(self, seq: int | None) -> SliReading | None:
        prior = [r for r in self.sli if seq is None or r.seq < seq]
        return prior[-1] if prior else None

    def succeeded_before(self, tools, seq: int | None) -> bool:
        return any(s < (seq if seq is not None else float("inf"))
                   for t in tools for s in self.succeeded_tools.get(t, []))

    def peak_error_before_declaration(self) -> float | None:
        rates = [r.error_rate_pct for r in self.sli
                 if self.declared_seq is None or r.seq < self.declared_seq]
        return max(rates) if rates else None

    def recovery_reading(self, threshold_pct: float) -> SliReading | None:
        """First healthy reading that follows at least one unhealthy reading."""
        seen_bad = False
        for r in self.sli:
            if r.error_rate_pct > threshold_pct:
                seen_bad = True
            elif seen_bad:
                return r
        return None


def extract_facts(trace: dict[str, Any], tools: ToolMap = ToolMap(),
                  responder_suffixes: tuple = ("-oncall",)) -> Facts:
    f = Facts(session_id=str(trace.get("session_id")))
    for idx, e in enumerate(trace.get("events", []), start=1):
        seq, ts, etype = e.get("seq", idx), _ts(e.get("ts")), e.get("type")
        if etype == "alert" and f.alert_text is None:
            f.alert_text, f.first_alert_ts = str(e.get("alert", "")), ts
        elif etype == "human_message":
            author = str(e.get("from", "unknown"))
            role = "responder" if author.endswith(responder_suffixes) else "stakeholder"
            key = f"{'resp' if role == 'responder' else 'stk'}_{seq}"
            f.messages.append(Message(key, seq, ts, role, author, str(e.get("text", ""))))
        elif etype == "tool_call":
            _tool_call(f, e, seq, ts, tools)
    final = trace.get("final_message")
    if isinstance(final, str):
        f.messages.append(Message("final", None, _ts(trace.get("ended_at")), "agent", "agent", final))
    return f


def _tool_call(f: Facts, e: dict, seq: int, ts, tools: ToolMap) -> None:
    tool, args, result = e.get("tool"), e.get("args") or {}, e.get("result")
    ok = isinstance(result, dict) and "error" not in result
    if ok:
        f.succeeded_tools.setdefault(tool, []).append(seq)
    if tool == tools.owner_tool and ok:
        f.owner_team, f.revenue_critical = result.get("owning_team"), result.get("revenue_critical")
    elif tool == tools.policy_tool and ok:
        f.policy.update(result)
    elif tool == tools.declare_tool and ok:
        f.declared_severity, f.declared_seq, f.declared_ts = args.get("severity"), seq, ts
    elif tool == tools.page_tool and ok and result.get("paged"):
        f.paged.append((seq, str(args.get("team"))))
    elif tool in tools.privileged:
        f.actions.append(Action(seq, tool, tools.privileged[tool], bool(args.get("approval_token")), ok))
    elif tool == tools.sli_tool and ok:
        rate = result.get(tools.sli_field)
        if isinstance(rate, (int, float)) and not isinstance(rate, bool):
            f.sli.append(SliReading(seq, ts, float(rate)))
    elif tool in tools.outbound and ok and result.get("delivered") is True:
        f.messages.append(Message(f"out_{seq}", seq, ts, "agent", "agent", str(args.get("body", ""))))
    elif tool == tools.close_tool and ok:
        f.closed_seq, f.closed_ts, f.close_resolution = seq, ts, args.get("resolution")


def later_events(trace: dict[str, Any], seq: int, limit: int = 12) -> list[str]:
    """Short, model-readable summaries of what happened after `seq` (context for one question)."""
    out: list[str] = []
    for e in trace.get("events", []):
        s = e.get("seq")
        if s is None or s <= seq:
            continue
        if e.get("type") == "tool_call":
            status = "error" if "error" in (e.get("result") or {}) else "ok"
            out.append(f"tool {e.get('tool')} {json.dumps(e.get('args') or {}, sort_keys=True)} -> {status}")
        else:
            out.append(f"{e.get('type')} from {e.get('from', 'system')}: {e.get('text', '')}")
    if isinstance(trace.get("final_message"), str):
        out.append(f"final message: {trace['final_message']}")
    return out[:limit]
