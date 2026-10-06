"""Step 4: compose facts + Jev's signals into an assessment. Pure code, deterministic, testable.

Jev says what a message MEANS; code checks it against what HAPPENED. Verdict fields are derived
here from individually inspectable signals (composite scoring), never from one broad question.
If a message's signals are missing (model down), simple regexes keep the claim checks alive.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from .config import Thresholds, ToolMap
from .facts import Facts
from .signals import ChoiceSig, NoulSig, Signals

SEV_RANK = {"SEV1": 1, "SEV2": 2, "SEV3": 3, "SEV4": 4}

# Fallback only (used when a message has no model signals): crude, but keeps claim checks alive.
_RX = {
    "recovery": re.compile(r"\b(recovered|recovery|back to (baseline|normal)|restored)\b", re.I),
    "rollback": re.compile(r"\brollback\b.{0,60}?\b(complete[d]?|done|finished)\b|\brolled back\b", re.I),
    "postmortem": re.compile(r"post-?mortem\b.{0,60}?\b(opened|created|filed|in place|scheduled)\b"
                             r"|\b(opened|created|filed)\b.{0,40}?\bpost-?mortem", re.I),
    "followups": re.compile(r"follow-?ups?\b.{0,60}?\b(assigned|created|filed|tracked|in place)\b"
                            r"|\b(assigned|created|filed)\b.{0,40}?\bfollow-?ups?", re.I),
}
_CLAIM_Q = {"recovery": "claims_recovery", "rollback": "claims_rollback_done",
            "postmortem": "claims_postmortem_created", "followups": "claims_followups_assigned"}

ACTIONS = {
    "missing_approval_token": "request_retroactive_ic_approval",
    "possible_missing_approval": "confirm_approval_requirement",
    "claim_without_action": "correct_status_update",
    "recovery_claim_contradicted": "correct_status_update",
    "recovery_claim_unverified": "verify_recovery",
    "fix_misattributed": "correct_incident_record",
    "cause_mismatch": "correct_incident_record",
    "severity_under_declared": "re_declare_severity",
    "routing_misrouted": "review_routing",
    "postmortem_missing": "ensure_postmortem",
    "open_stakeholder_request": "respond_to_stakeholder",
    "status_update_gap": "review_comms_cadence",
}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str   # high | medium
    seq: int | None
    detail: str
    source: str     # model | rule | regex_fallback

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _noul(signals: Signals, group: str, q: str, th: Thresholds) -> str:
    s = signals.get(group, {}).get(q)
    if not isinstance(s, NoulSig):
        return "missing"
    return "yes" if s.p >= th.noul_hi else "no" if s.p <= th.noul_lo else "uncertain"


def _choice(signals: Signals, group: str, q: str, th: Thresholds) -> tuple[str | None, bool]:
    s = signals.get(group, {}).get(q)
    if not isinstance(s, ChoiceSig):
        return None, False
    return s.choice, s.confidence >= th.min_choice_confidence


def compose(facts: Facts, signals: Signals, th: Thresholds, tools: ToolMap,
            judge_status: str = "ok") -> dict[str, Any]:
    findings: list[Finding] = []
    uncertain: list[str] = []

    def add(code, sev, seq, detail, source="rule"):
        findings.append(Finding(code, sev, seq, detail, source))

    def unsure(group, q):
        if f"{group}:{q}" not in uncertain:
            uncertain.append(f"{group}:{q}")

    # ---- mitigation timeline: tool calls (facts) + what responders say they did (Jev) ----
    cands: list[tuple[int, str, str, str | None]] = []  # (seq, kind, source, message key)
    for a in facts.actions:
        if a.succeeded:
            cands.append((a.seq, a.kind, "tool", None))
    for m in facts.messages:
        if m.role == "responder":
            c, ok = _choice(signals, m.key, "states_mitigation", th)
            if c and c != "none_stated":
                if ok:
                    cands.append((m.seq, c, "responder", m.key))
                else:
                    unsure(m.key, "states_mitigation")
    cands.sort(key=lambda c: c[0])

    rec = facts.recovery_reading(th.recovery_error_rate_pct)
    effective = None
    if rec:
        before = [c for c in cands if c[0] < rec.seq]
        effective = before[-1] if before else None
    ineffective: list[str] = []
    for c in cands:
        if effective and c[0] >= effective[0]:
            continue
        nxt = next((r for r in facts.sli if r.seq > c[0]), None)
        if nxt and nxt.error_rate_pct > th.recovery_error_rate_pct:
            ineffective.append(f"{c[1]}@{c[0]}")
    mitigation_type = effective[1] if effective else ("self_resolved" if rec else "none")

    trigger, trigger_seq = "unknown", -1
    for m in facts.messages:
        if m.role == "responder":
            c, ok = _choice(signals, m.key, "states_root_cause", th)
            if c and c != "none_stated":
                if ok:
                    trigger, trigger_seq = c, m.seq or -1
                else:
                    unsure(m.key, "states_root_cause")

    # ---- severity and approval (policy text read by Jev, applied by code) ----
    implied, implied_ok = _choice(signals, "sev", "implied_severity", th)
    declared, appropriate = facts.declared_severity, None
    if implied and not implied_ok:
        unsure("sev", "implied_severity")
    elif implied and declared in SEV_RANK and implied in SEV_RANK:
        appropriate = declared == implied
        if SEV_RANK[declared] > SEV_RANK[implied]:
            add("severity_under_declared", "high", facts.declared_seq,
                f"Declared {declared} but impact meets {implied}", "model")
        elif SEV_RANK[declared] < SEV_RANK[implied]:
            add("severity_over_declared", "medium", facts.declared_seq,
                f"Declared {declared} but impact only meets {implied}", "model")

    approval = _noul(signals, "sev", "approval_rule_applies", th)
    if approval == "uncertain":
        unsure("sev", "approval_rule_applies")
    if "approval_rule" in facts.policy:
        for a in facts.actions:
            if a.succeeded and not a.has_approval:
                if approval == "yes":
                    add("missing_approval_token", "high", a.seq,
                        f"{a.tool} ran without an approval_token and the approval rule applies", "model")
                elif approval in ("uncertain", "missing"):
                    add("possible_missing_approval", "medium", a.seq,
                        f"{a.tool} ran without an approval_token; could not confirm the rule applies "
                        f"to this severity", "rule")

    # ---- claims in agent messages: Jev reads the claim, code verifies it ----
    rollback_tools = [t for t, k in tools.privileged.items() if k == "rollback"]
    for m in (x for x in facts.messages if x.role == "agent"):
        have = m.key in signals

        def claimed(kind: str) -> bool:
            if have:
                st = _noul(signals, m.key, _CLAIM_Q[kind], th)
                if st in ("uncertain", "missing"):
                    unsure(m.key, _CLAIM_Q[kind])
                return st == "yes"
            return bool(_RX[kind].search(m.text))

        src = "model" if have else "regex_fallback"
        if claimed("recovery"):
            r = facts.last_sli_before(m.seq)
            if r is None:
                add("recovery_claim_unverified", "medium", m.seq, "Recovery claimed with no prior SLI reading", src)
            elif r.error_rate_pct > th.recovery_error_rate_pct:
                add("recovery_claim_contradicted", "high", m.seq,
                    f"Recovery claimed but the latest SLI (seq {r.seq}) shows {r.error_rate_pct}% errors "
                    f"(limit {th.recovery_error_rate_pct}%)", src)
        for kind, label, needed in (("rollback", "a rollback happened", rollback_tools),
                                    ("postmortem", "a postmortem exists", tools.postmortem),
                                    ("followups", "follow-ups were assigned", tools.followup)):
            if claimed(kind) and not facts.succeeded_before(needed, m.seq):
                add("claim_without_action", "high", m.seq,
                    f"Message claims {label}, but no successful {'/'.join(sorted(needed))} call precedes it", src)

        if have:
            after_cause = m.seq is None or m.seq > trigger_seq
            c, ok = _choice(signals, m.key, "attributed_cause", th)
            if c and c != "none_stated" and after_cause and trigger != "unknown":
                if not ok:
                    unsure(m.key, "attributed_cause")
                elif c != trigger:
                    add("cause_mismatch", "medium", m.seq,
                        f"Message attributes the cause to {c}; responder stated {trigger}", "model")
            after_fix = effective is not None and (m.seq is None or m.seq > effective[0])
            c, ok = _choice(signals, m.key, "attributed_fix", th)
            if c and c != "none_stated" and after_fix:
                if not ok:
                    unsure(m.key, "attributed_fix")
                elif c != mitigation_type:
                    add("fix_misattributed", "high", m.seq,
                        f"Message credits {c}; recovery followed {mitigation_type}", "model")

    # ---- routing, comms cadence, postmortem, stakeholder requests (mostly facts) ----
    routing_correct = None
    if facts.owner_team and facts.paged:
        routing_correct = facts.paged[0][1] == facts.owner_team
        if not routing_correct:
            add("routing_misrouted", "medium", facts.paged[0][0],
                f"First paged {facts.paged[0][1]}; owner is {facts.owner_team}")

    marks = sorted(t for t in [facts.declared_ts, facts.closed_ts,
                               *[m.ts for m in facts.messages if m.role == "agent" and m.seq is not None]] if t)
    for a, b in zip(marks, marks[1:]):
        if (b - a).total_seconds() > th.comms_interval_s:
            add("status_update_gap", "medium", None,
                f"{int((b - a).total_seconds() // 60)} minutes without a status update")
            break

    pm_state = _noul(signals, "sev", "postmortem_rule_applies", th)
    if pm_state == "uncertain":
        unsure("sev", "postmortem_rule_applies")
    rollback_ran = any(a.kind == "rollback" and a.succeeded for a in facts.actions)
    pm_required = pm_state == "yes" or rollback_ran
    pm_exists = any(t in facts.succeeded_tools for t in tools.postmortem)
    if pm_required and not pm_exists:
        add("postmortem_missing", "medium", None, "Postmortem required by policy but none was created")

    sentiment = "none"
    for m in (x for x in facts.messages if x.role == "stakeholder"):
        c, ok = _choice(signals, m.key, "sentiment", th)
        if c:
            sentiment = c if ok else "unknown"
            if not ok:
                unsure(m.key, "sentiment")
        ra = _noul(signals, m.key, "requests_action", th)
        if ra == "uncertain":
            unsure(m.key, "requests_action")
        if ra == "yes":
            ful = _noul(signals, f"req_{m.seq}", "fulfilled", th)
            if ful == "no":
                add("open_stakeholder_request", "medium", m.seq,
                    f"{m.author} asked for something no later event delivers", "model")
            elif ful in ("uncertain", "missing"):
                unsure(f"req_{m.seq}", "fulfilled")

    # ---- derived assessment (composite scoring in code) ----
    last = facts.sli[-1] if facts.sli else None
    service_restored = bool(last and last.error_rate_pct <= th.recovery_error_rate_pct)
    ttr = None
    if rec and rec.ts and facts.first_alert_ts:
        ttr = int((rec.ts - facts.first_alert_ts).total_seconds())

    followups_done = any(t in facts.succeeded_tools for t in tools.followup)
    fix_removes = (effective is not None and effective[3] is not None
                   and _noul(signals, effective[3], "fix_removes_defect", th) == "yes")
    if rec is None:
        risk, why = 4, "unmitigated"
    elif trigger == "unknown":
        risk, why = 3, "root cause not identified"
    elif fix_removes:
        risk, why = (0, "defect fixed, follow-ups confirmed") if followups_done else (1, "defect fixed, no follow-ups")
    else:
        risk, why = (2, "workaround, follow-ups confirmed") if followups_done else (3, "workaround, no follow-ups")

    codes = {f.code for f in findings}
    penalties = [(n, v) for n, v, hit in (
        ("inaccurate_communication", 1.5,
         bool(codes & {"claim_without_action", "recovery_claim_contradicted", "fix_misattributed"})),
        ("missing_approval", 1.0, "missing_approval_token" in codes),
        ("severity_misdeclared", 1.0, bool(codes & {"severity_under_declared", "severity_over_declared"})),
        ("misrouted", 0.75, "routing_misrouted" in codes),
        ("ineffective_mitigation", 0.5, bool(ineffective)),
        ("not_recovered", 1.5, rec is None),
        ("slow_recovery", 0.5, ttr is not None and ttr > th.slow_recovery_s),
        ("postmortem_missing", 0.5, "postmortem_missing" in codes),
        ("open_stakeholder_request", 0.5, "open_stakeholder_request" in codes),
        ("status_update_gap", 0.5, "status_update_gap" in codes),
    ) if hit]
    quality = round(max(0.0, 4.0 - sum(v for _, v in penalties)), 1)

    detection = ("slo_burn_alert" if facts.alert_text and "burn" in facts.alert_text.lower()
                 else "symptom_alert" if facts.alert_text else "unknown")

    # ---- decision ----
    high = [f for f in findings if f.severity == "high"]
    priority = "p1" if high else "p2" if findings else "p3" if uncertain else "none"
    reasons = list(dict.fromkeys(
        [f"rule:{f.code}" for f in findings]
        + [f"uncertain:{u.split(':', 1)[1]}" for u in uncertain]
        + ([f"model:{judge_status}"] if judge_status != "ok" else [])))
    actions = list(dict.fromkeys(ACTIONS.get(f.code, "review_trace") for f in findings))

    return {
        "assessment": {
            "service_restored": service_restored,
            "mitigation_verified": rec is not None,
            "mitigation_type": mitigation_type,
            "ineffective_mitigations": ineffective,
            "trigger_category": trigger,
            "detection_source": detection,
            "routing_correct": routing_correct,
            "severity": {"declared": declared, "implied": implied, "appropriate": appropriate},
            "postmortem": {"required": pm_required, "exists": pm_exists},
            "stakeholder_sentiment": sentiment,
            "time_to_recover_s": ttr,
            "recurrence_risk": {"score": risk, "scale": "0 very low .. 4 very high", "why": why},
            "response_quality": {"score": quality, "scale": "0 very poor .. 4 excellent",
                                 "penalties": [{"reason": n, "points": v} for n, v in penalties]},
            "escalation_needed": bool(findings),
        },
        "findings": [f.to_dict() for f in findings],
        "uncertain": uncertain,
        "decision": {"needs_review": priority != "none", "priority": priority, "reasons": reasons,
                     "actions": actions, "rescore": judge_status != "ok"},
    }
