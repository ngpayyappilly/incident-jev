from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Thresholds:
    noul_hi: float = 0.8            # P(yes) at/above: treat as yes
    noul_lo: float = 0.2            # P(yes) at/below: treat as no; in between = uncertain
    min_choice_confidence: float = 0.7
    recovery_error_rate_pct: float = 1.0  # replace with a per-service baseline from your metrics store
    slow_recovery_s: int = 1800
    comms_interval_s: int = 1800


@dataclass(frozen=True)
class ToolMap:
    """Names of the tools in YOUR incident agent's traces. Adapt to your stack."""
    owner_tool: str = "get_service_owner"
    policy_tool: str = "get_policy"
    declare_tool: str = "declare_incident"
    page_tool: str = "page_team"
    close_tool: str = "close_incident"
    sli_tool: str = "query_slo"
    sli_field: str = "error_rate_5m_pct"
    privileged: dict = field(default_factory=lambda: {   # tool -> mitigation kind
        "rollback_deploy": "rollback",
        "shift_traffic": "failover_or_traffic_shift",
        "failover_region": "failover_or_traffic_shift",
    })
    outbound: frozenset = frozenset({"send_status_update", "send_message"})
    postmortem: frozenset = frozenset({"create_postmortem"})
    followup: frozenset = frozenset({"create_ticket", "create_action_item", "assign_followup"})


def _req(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


@dataclass(frozen=True)
class Settings:
    model: str | None            # pin an exact Jev version in production
    timeout_s: float
    max_retries: int
    backoff_max_s: float
    breaker_failures: int
    breaker_cooldown_s: float
    max_concurrency: int
    max_questions_per_request: int
    cache_ttl_s: float
    cache_max_items: int
    api_key: str                 # inbound auth for this service; empty disables (local dev only)
    responder_suffixes: tuple
    thresholds: Thresholds
    tools: ToolMap

    @classmethod
    def from_env(cls) -> "Settings":
        _req("TYPESAFE_API_KEY")  # fail fast; the SDK reads it from the environment itself
        e = os.environ.get
        return cls(
            model=e("JEV_MODEL") or None,
            timeout_s=float(e("JEV_TIMEOUT_S", "5")),
            max_retries=int(e("JEV_MAX_RETRIES", "2")),
            backoff_max_s=float(e("JEV_BACKOFF_MAX_S", "1.0")),
            breaker_failures=int(e("BREAKER_FAILURES", "5")),
            breaker_cooldown_s=float(e("BREAKER_COOLDOWN_S", "30")),
            max_concurrency=int(e("MAX_CONCURRENCY", "16")),
            max_questions_per_request=int(e("MAX_QUESTIONS_PER_REQUEST", "40")),
            cache_ttl_s=float(e("CACHE_TTL_S", "3600")),
            cache_max_items=int(e("CACHE_MAX_ITEMS", "10000")),
            api_key=e("EVAL_API_KEY", ""),
            responder_suffixes=tuple(s for s in e("RESPONDER_SUFFIXES", "-oncall").split(",") if s),
            thresholds=Thresholds(
                noul_hi=float(e("TH_NOUL_HI", "0.8")),
                noul_lo=float(e("TH_NOUL_LO", "0.2")),
                min_choice_confidence=float(e("TH_MIN_CHOICE_CONF", "0.7")),
                recovery_error_rate_pct=float(e("TH_RECOVERY_ERROR_PCT", "1.0")),
                slow_recovery_s=int(e("TH_SLOW_RECOVERY_S", "1800")),
                comms_interval_s=int(e("TH_COMMS_INTERVAL_S", "1800")),
            ),
            tools=ToolMap(),
        )
