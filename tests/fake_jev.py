"""A keyword-driven stand-in for the SDK client. It tests PLUMBING and decision logic only.
It says nothing about Jev's real accuracy: tune questions against the real model."""
import re
from types import SimpleNamespace

SEP = "__"


class TypeSafeInternalServerError(Exception): ...
class TypeSafeAuthenticationError(Exception): ...
class TypeSafeAPIResponseValidationError(Exception): ...


def _n(p): return SimpleNamespace(noul=p)
def _c(choice, conf): return SimpleNamespace(choice=choice, confidence=conf)


class FakeJev:
    def __init__(self, fail_with=None, drop=()):
        self.calls, self.fail_with, self.drop = [], fail_with, set(drop)

    async def system_one(self, state, questions):
        self.calls.append((state, questions))
        if self.fail_with:
            raise self.fail_with()
        nouls, choices = {}, {}
        for name, q in questions.items():
            group, qn = name.split(SEP, 1)
            if qn in self.drop:
                continue
            body = (state.get(group) or {}).get("body", "").lower()
            if q["type"] == "noul":
                nouls[name] = _n(self._noul(qn, group, body, state))
            else:
                choices[name] = self._choice(qn, group, body, state)
        return SimpleNamespace(nouls=nouls, choices=choices, scores={}, model="jev-fake",
                               request_id=f"req_{len(self.calls)}",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=10))

    def _noul(self, q, group, body, state):
        hit = lambda c: 0.95 if c else 0.03
        if q == "claims_recovery": return hit(re.search(r"recovered|back to baseline|restored", body))
        if q == "claims_rollback_done": return hit(re.search(r"rollback.{0,60}complete|rolled back", body))
        if q == "claims_postmortem_created": return hit(re.search(r"postmortem.{0,60}(opened|in place)", body))
        if q == "claims_followups_assigned": return hit(re.search(r"follow-?ups?.{0,60}(assigned|in place)", body))
        if q == "says_prior_action_ineffective": return hit("did nothing" in body)
        if q == "fix_removes_defect": return 0.08
        if q == "requests_action": return hit("can someone" in body)
        if q == "fulfilled": return 0.05
        if q in ("approval_rule_applies", "postmortem_rule_applies"): return 0.93
        raise AssertionError(q)

    def _choice(self, q, group, body, state):
        if q == "implied_severity":
            hot = state["impact"]["peak_error_rate_pct"] > 5 and state["impact"]["revenue_critical_path"]
            return _c("SEV1" if hot else "SEV3", 0.95)
        if q == "attributed_cause":
            return _c("change_deploy", 0.85) if re.search(r"deploy|rolled back", body) else _c("none_stated", 0.9)
        if q == "attributed_fix":
            return _c("rollback", 0.9) if "rolled back" in body else _c("none_stated", 0.9)
        if q == "states_root_cause":
            return _c("config_flag", 0.9) if "flag" in body else _c("none_stated", 0.8)
        if q == "states_mitigation":
            return _c("flag_or_config_revert", 0.9) if "turned that flag off" in body else _c("none_stated", 0.9)
        if q == "sentiment":
            return _c("positive", 0.55)
        raise AssertionError(q)
