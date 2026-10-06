"""Step 2: turn facts into narrow, typed questions over minimal state. Pure code, no SDK import.

Design rules taken from TypeSafe's guidance:
  * each question is atomic and points at one value in `state` via a backticked path;
  * each message gets only its own text as context (no whole-trace dumps);
  * questions over the same state are batched into one request, requests run in parallel;
  * structured instructions/criteria say what counts and what does not.
Questions are plain dicts ({"type": "noul"|"choice", ...}), which the SDK accepts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Thresholds
from .facts import Facts, Message, later_events
from .signals import SEP, NoulSig, Signals

TRIGGER_OPTIONS = {
    "config_flag": "A configuration change or feature-flag change",
    "change_deploy": "A code or artifact deployment",
    "capacity": "Load, saturation, or resource exhaustion",
    "dependency": "A failing upstream, downstream, or third-party dependency",
    "infrastructure": "A platform, network, hardware, or cloud-provider fault",
    "security": "A security event or abuse",
    "data": "Bad data or a data-pipeline problem",
    "none_stated": "No cause is stated; a request to investigate does not count as a stated cause",
}
MITIGATION_OPTIONS = {
    "rollback": "Reverting a deployment",
    "flag_or_config_revert": "Disabling a feature flag or reverting a configuration",
    "failover_or_traffic_shift": "Moving traffic, failing over, or draining a region or backend",
    "scale_or_capacity": "Adding capacity, raising limits, or shedding load",
    "restart": "Restarting or recycling processes or nodes",
    "none_stated": "No mitigating action is described as performed",
}
SENTIMENT_OPTIONS = {
    "positive": "Pleased, grateful, or reassured",
    "neutral": "Matter-of-fact",
    "negative": "Frustrated, alarmed, or dissatisfied",
    "mixed": "Clearly conflicting feelings",
}


def noul(question: str, *, true: str, false: str, focus: str | None = None) -> dict:
    instructions: dict[str, Any] = {"question": question}
    if focus:
        instructions["focus"] = focus
    return {"type": "noul", "instructions": instructions, "criteria": {"true": true, "false": false}}


def choice(question: str, options: dict[str, str], focus: str | None = None) -> dict:
    instructions: dict[str, Any] = {"question": question}
    if focus:
        instructions["focus"] = focus
    return {"type": "choice", "instructions": instructions, "criteria": dict(options)}


def qname(group: str, name: str) -> str:
    return f"{group}{SEP}{name}"


@dataclass
class Probe:
    group: str
    state: dict
    questions: dict


# ---------------------------------------------------------------- per-message probes

def agent_probe(m: Message) -> Probe:
    p = f"{m.key}.body"
    qs = {
        "claims_recovery": noul(
            f"Does `{p}` state that service health has recovered or error rates are back to normal?",
            true="Asserts recovery as a present fact",
            false="No recovery claim, or only says recovery is expected or being monitored",
            focus="Judge what is asserted, not what is likely true."),
        "claims_rollback_done": noul(
            f"Does `{p}` state that a rollback or revert has been completed?",
            true="Says a rollback or revert already happened",
            false="No rollback claim, or a rollback is only planned or suggested"),
        "claims_postmortem_created": noul(
            f"Does `{p}` state that a postmortem document or ticket has been opened or created?",
            true="Says a postmortem exists or is in place",
            false="No such claim, or a postmortem is only mentioned as a future step"),
        "claims_followups_assigned": noul(
            f"Does `{p}` state that follow-up action items have been created or assigned?",
            true="Says follow-ups exist, are assigned, or are in place",
            false="No such claim, or follow-ups are only mentioned as a future step"),
        "attributed_cause": choice(
            f"What cause does `{p}` attribute the incident to?", TRIGGER_OPTIONS,
            focus="A hypothesis phrased as a suspicion still counts as the attributed cause."),
        "attributed_fix": choice(
            f"Which action does `{p}` credit with resolving or mitigating the incident?",
            MITIGATION_OPTIONS,
            focus="Only an action the message credits with the recovery; merely mentioning an action does not count."),
    }
    return Probe(m.key, {m.key: {"body": m.text}}, {qname(m.key, k): v for k, v in qs.items()})


def responder_probe(m: Message) -> Probe:
    p = f"{m.key}.body"
    qs = {
        "states_root_cause": choice(f"What root cause does the author of `{p}` state?", TRIGGER_OPTIONS),
        "states_mitigation": choice(
            f"Which mitigating action does the author of `{p}` say they performed?", MITIGATION_OPTIONS),
        "says_prior_action_ineffective": noul(
            f"Does `{p}` say that an earlier action or diagnosis did not help or was wrong?",
            true="Explicitly says a previous action had no effect or the diagnosis was wrong",
            false="Does not comment on whether earlier actions worked"),
        "fix_removes_defect": noul(
            f"Does `{p}` describe a change that eliminates the underlying defect, rather than only "
            f"disabling a feature or working around it?",
            true="A permanent fix of the defect itself",
            false="A workaround, disablement, rollback, or no fix described"),
    }
    return Probe(m.key, {m.key: {"author": m.author, "body": m.text}},
                 {qname(m.key, k): v for k, v in qs.items()})


def stakeholder_probe(m: Message) -> Probe:
    p = f"{m.key}.body"
    qs = {
        "sentiment": choice(f"What is the sentiment of `{p}`?", SENTIMENT_OPTIONS),
        "requests_action": noul(
            f"Does `{p}` ask the responders to do or provide something?",
            true="Contains a request addressed to the response team",
            false="Only thanks, comments, or informs"),
    }
    return Probe(m.key, {m.key: {"author": m.author, "body": m.text}},
                 {qname(m.key, k): v for k, v in qs.items()})


def severity_probe(f: Facts) -> Probe | None:
    """Policy text is natural language: let Jev read it, with the policy supplied in state."""
    defs, rate = f.policy.get("severity_definitions"), f.peak_error_before_declaration()
    if not defs or rate is None:
        return None
    policy = {k: f.policy[k] for k in ("severity_definitions", "approval_rule", "postmortem_rule")
              if k in f.policy}
    state = {"policy": policy,
             "impact": {"peak_error_rate_pct": rate, "revenue_critical_path": f.revenue_critical}}
    qs = {"implied_severity": choice(
        "Which severity level in `policy.severity_definitions` does `impact` meet?",
        {k: f"`impact` meets the `policy.severity_definitions.{k}` definition" for k in defs},
        focus="Choose the most severe level whose definition is met.")}
    if "approval_rule" in policy:
        qs["approval_rule_applies"] = noul(
            "Given `policy.severity_definitions` and `impact`, does `policy.approval_rule` require "
            "an approval token for production changes in this incident?",
            true="The incident's severity falls within the severities the rule covers",
            false="The incident's severity is outside the rule's scope")
    if "postmortem_rule" in policy:
        qs["postmortem_rule_applies"] = noul(
            "Given `policy.severity_definitions` and `impact`, does `policy.postmortem_rule` require "
            "a postmortem for this incident?",
            true="The severity-based condition of the rule is met",
            false="The severity-based condition of the rule is not met")
    return Probe("sev", state, {qname("sev", k): v for k, v in qs.items()})


def build_stage1(f: Facts) -> list[Probe]:
    probes: list[Probe] = []
    sev = severity_probe(f)
    if sev:
        probes.append(sev)
    builders = {"agent": agent_probe, "responder": responder_probe, "stakeholder": stakeholder_probe}
    probes += [builders[m.role](m) for m in f.messages]
    return probes


# ---------------------------------------------------------------- stage 2 (dependent)

def build_stage2(f: Facts, trace: dict[str, Any], signals: Signals, th: Thresholds) -> list[Probe]:
    """Only ask 'was it fulfilled?' for requests stage 1 identified. Context: later events only."""
    probes: list[Probe] = []
    for m in f.messages:
        sig = signals.get(m.key, {}).get("requests_action")
        if m.role == "stakeholder" and m.seq is not None and isinstance(sig, NoulSig) and sig.p >= th.noul_hi:
            g = f"req_{m.seq}"
            probes.append(Probe(g, {g: {"request": m.text, "later_events": later_events(trace, m.seq)}}, {
                qname(g, "fulfilled"): noul(
                    f"Do `{g}.later_events` show that the request in `{g}.request` was fulfilled?",
                    true="A later event delivers, or concretely schedules, what was requested",
                    false="No later event addresses the request; closing the incident does not count")}))
    return probes


def pack(probes: list[Probe], max_questions: int) -> list[tuple[dict, dict]]:
    """Merge probes into as few requests as the per-request question budget allows."""
    batches: list[tuple[dict, dict]] = []
    state: dict = {}
    qs: dict = {}
    for p in probes:
        if qs and len(qs) + len(p.questions) > max_questions:
            batches.append((state, qs))
            state, qs = {}, {}
        state.update(p.state)
        qs.update(p.questions)
    if qs:
        batches.append((state, qs))
    return batches
