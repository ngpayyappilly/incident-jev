import asyncio
import copy
import json
import unittest
from pathlib import Path

from app.config import Thresholds, ToolMap
from app.decide import compose
from app.facts import extract_facts
from app.jev_gateway import JevGateway, JevUnavailable, JevRejected, classify
from app.judge import Judge
from app.probes import build_stage1, pack
from app.resilience import CircuitBreaker, SingleFlightCache
from app.service import EvaluationService
from app.signals import SEP
from tests.fake_jev import (FakeJev, TypeSafeAuthenticationError, TypeSafeInternalServerError,
                            TypeSafeAPIResponseValidationError)

TRACE = json.loads((Path(__file__).parent / "fixtures" / "trace.json").read_text())
TH, TOOLS = Thresholds(), ToolMap()


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


def make_service(client, *, max_q=40, breaker=None):
    gw = JevGateway(client, breaker=breaker or CircuitBreaker(5, 30), max_concurrency=4)
    return EvaluationService(judge=Judge(gw, TH, max_q), thresholds=TH, tools=TOOLS,
                             cache=SingleFlightCache(60, 100))


class FactsTests(unittest.TestCase):
    def setUp(self): self.f = extract_facts(TRACE, TOOLS)

    def test_extraction(self):
        f = self.f
        self.assertEqual((f.owner_team, f.declared_severity, f.revenue_critical), ("payments-core", "SEV3", True))
        self.assertEqual([t for _, t in f.paged], ["platform-networking", "payments-core"])
        self.assertEqual([(a.seq, a.kind, a.has_approval) for a in f.actions], [(12, "rollback", False)])
        self.assertEqual([round(r.error_rate_pct, 1) for r in f.sli], [6.2, 5.8, 0.3])
        self.assertEqual({m.key: m.role for m in f.messages},
                         {"out_11": "agent", "out_14": "agent", "out_17": "agent", "final": "agent",
                          "resp_8": "responder", "resp_15": "responder", "stk_18": "stakeholder"})
        keys = {m.key for m in f.messages}
        self.assertEqual(keys, {"out_11", "out_14", "out_17", "resp_8", "resp_15", "stk_18", "final"})

    def test_failed_message_is_excluded(self):
        self.assertNotIn("out_10", {m.key for m in self.f.messages})  # gateway timeout, never delivered

    def test_helpers(self):
        f = self.f
        self.assertEqual(f.peak_error_before_declaration(), 6.2)
        self.assertEqual(f.recovery_reading(1.0).seq, 16)
        self.assertEqual(f.last_sli_before(14).seq, 13)
        self.assertTrue(f.succeeded_before({"rollback_deploy"}, 14))
        self.assertFalse(f.succeeded_before({"create_postmortem"}, None))


class ProbeTests(unittest.TestCase):
    def test_questions_are_atomic_unique_and_paths_resolve(self):
        f = extract_facts(TRACE, TOOLS)
        probes = build_stage1(f)
        names = [n for p in probes for n in p.questions]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(SEP in n for n in names))
        for p in probes:
            for q in p.questions.values():
                text = json.dumps(q["instructions"])
                for path in __import__("re").findall(r"`([^`]+)`", text):
                    root = path.split(".")[0]
                    self.assertIn(root, p.state, f"{path} not in state of {p.group}")

    def test_pack_respects_budget_and_keeps_groups_together(self):
        probes = build_stage1(extract_facts(TRACE, TOOLS))
        total = sum(len(p.questions) for p in probes)
        one = pack(probes, 1000)
        self.assertEqual((len(one), len(one[0][1])), (1, total))
        small = pack(probes, 8)
        self.assertGreater(len(small), 1)
        for state, qs in small:
            for name in qs:
                group = name.split(SEP)[0]
                needed = ("policy", "impact") if group == "sev" else (group,)
                for key in needed:
                    self.assertIn(key, state)   # a question's state travels with it
        self.assertEqual(sum(len(q) for _, q in small), total)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_pipeline_on_sample_incident(self):
        fake = FakeJev()
        v = await make_service(fake).evaluate(TRACE)
        a, codes = v["assessment"], [(f["code"], f["seq"]) for f in v["findings"]]
        self.assertEqual(v["status"], "ok")
        self.assertEqual(v["decision"]["priority"], "p1")
        self.assertEqual((a["mitigation_type"], a["trigger_category"], a["detection_source"]),
                         ("flag_or_config_revert", "config_flag", "slo_burn_alert"))
        self.assertEqual(a["ineffective_mitigations"], ["rollback@12"])
        self.assertEqual((a["severity"]["declared"], a["severity"]["implied"]), ("SEV3", "SEV1"))
        self.assertFalse(a["routing_correct"])
        self.assertTrue(a["service_restored"]); self.assertTrue(a["mitigation_verified"])
        self.assertEqual(a["recurrence_risk"]["score"], 3)
        self.assertEqual(a["stakeholder_sentiment"], "unknown")      # 0.55 confidence < 0.7
        self.assertIn("stk_18:sentiment", v["uncertain"])
        self.assertEqual(a["time_to_recover_s"], 2057)
        for expected in [("missing_approval_token", 12), ("recovery_claim_contradicted", 14),
                         ("claim_without_action", 17), ("claim_without_action", None),
                         ("fix_misattributed", None), ("severity_under_declared", 6),
                         ("routing_misrouted", 7), ("postmortem_missing", None),
                         ("open_stakeholder_request", 18)]:
            self.assertIn(expected, codes)
        self.assertNotIn(("recovery_claim_contradicted", 17), codes)   # 0.3% is healthy
        self.assertEqual(a["response_quality"]["score"], 0.0)
        self.assertTrue(a["escalation_needed"])
        self.assertIn("respond_to_stakeholder", v["decision"]["actions"])

    async def test_fan_out_uses_few_requests_and_stage2_only_when_needed(self):
        fake = FakeJev()
        v = await make_service(fake).evaluate(TRACE)
        self.assertEqual(v["jev"]["requests"], 2)           # stage 1 (one batch) + stage 2 (one request)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(list(fake.calls[1][1]), ["req_18__fulfilled"])
        self.assertEqual(sorted(fake.calls[1][0]["req_18"]), ["later_events", "request"])
        clean = copy.deepcopy(TRACE)
        clean["events"] = [e for e in clean["events"] if e["seq"] != 18]
        fake2 = FakeJev()
        await make_service(fake2).evaluate(clean)
        self.assertEqual(len(fake2.calls), 1)                # no request -> no stage 2

    async def test_small_budget_splits_requests(self):
        fake = FakeJev()
        v = await make_service(fake, max_q=8).evaluate(TRACE)
        self.assertGreater(len(fake.calls), 3)
        self.assertEqual(v["status"], "ok")
        self.assertEqual(v["assessment"]["mitigation_type"], "flag_or_config_revert")

    async def test_idempotent_cache(self):
        fake = FakeJev(); svc = make_service(fake)
        v1 = await svc.evaluate(TRACE); n = len(fake.calls); v2 = await svc.evaluate(TRACE)
        self.assertEqual((v1["cached"], v2["cached"], len(fake.calls)), (False, True, n))
        self.assertEqual(v1["evaluation_id"], v2["evaluation_id"])

    async def test_degraded_when_jev_down_keeps_rule_findings(self):
        fake = FakeJev(fail_with=TypeSafeInternalServerError)
        svc = make_service(fake)
        v = await svc.evaluate(TRACE)
        self.assertEqual(v["status"], "degraded")
        self.assertTrue(v["decision"]["rescore"])
        codes = {f["code"] for f in v["findings"]}
        # no model: regex fallback still catches the unbacked claims; facts still catch routing/approval
        self.assertTrue({"claim_without_action", "recovery_claim_contradicted",
                         "routing_misrouted", "possible_missing_approval", "postmortem_missing"} <= codes)
        self.assertIn("regex_fallback", {f["source"] for f in v["findings"] if f["code"] == "claim_without_action"})
        self.assertEqual(v["decision"]["priority"], "p1")
        n = len(fake.calls); await svc.evaluate(TRACE)
        self.assertGreater(len(fake.calls), n)               # degraded verdict not cached

    async def test_degraded_clean_trace_is_rescored_not_reviewed(self):
        v = await make_service(FakeJev(fail_with=TypeSafeInternalServerError)).evaluate(
            {"session_id": "s", "events": [], "final_message": "Done."})
        self.assertEqual(v["status"], "degraded")
        self.assertEqual((v["decision"]["priority"], v["decision"]["rescore"]), ("none", True))

    async def test_trace_with_nothing_to_ask_never_calls_the_model(self):
        fake = FakeJev(fail_with=TypeSafeInternalServerError)
        v = await make_service(fake).evaluate({"session_id": "s", "events": []})
        self.assertEqual((v["status"], len(fake.calls), v["decision"]["priority"]), ("ok", 0, "none"))

    async def test_partial_when_model_skips_questions(self):
        v = await make_service(FakeJev(drop={"claims_postmortem_created"})).evaluate(TRACE)
        self.assertEqual(v["status"], "partial")
        self.assertTrue(v["decision"]["rescore"])
        self.assertTrue(any(u.endswith("claims_postmortem_created") for u in v["jev"]["unanswered"]))


class ComposeTests(unittest.TestCase):
    def test_uncertain_only_is_p3(self):
        from app.facts import Facts, Message
        from app.signals import ChoiceSig, NoulSig
        f = Facts(session_id="s", messages=[Message("stk_1", 1, None, "stakeholder", "x", "thanks")])
        sig = {"stk_1": {"sentiment": ChoiceSig("positive", 0.4), "requests_action": NoulSig(0.05)}}
        out = compose(f, sig, TH, TOOLS)
        self.assertEqual((out["decision"]["priority"], out["decision"]["reasons"]),
                         ("p3", ["uncertain:sentiment"]))
        self.assertEqual(out["findings"], [])


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def test_classify(self):
        self.assertEqual(classify(TypeSafeInternalServerError()), "transient")
        self.assertEqual(classify(TypeSafeAuthenticationError()), "rejected")
        self.assertEqual(classify(TypeSafeAPIResponseValidationError()), "bad_response")
        self.assertIsNone(classify(ValueError()))

    async def test_breaker_opens_then_short_circuits(self):
        fake = FakeJev(fail_with=TypeSafeInternalServerError)
        gw = JevGateway(fake, breaker=CircuitBreaker(2, 30), max_concurrency=2)
        for _ in range(2):
            with self.assertRaises(JevUnavailable): await gw.ask({}, {})
        with self.assertRaises(JevUnavailable) as e: await gw.ask({}, {})
        self.assertEqual(e.exception.reason, "circuit_open")
        self.assertEqual(len(fake.calls), 2)

    async def test_rejected_does_not_trip_breaker(self):
        b = CircuitBreaker(1, 30)
        gw = JevGateway(FakeJev(fail_with=TypeSafeAuthenticationError), breaker=b, max_concurrency=2)
        with self.assertRaises(JevRejected): await gw.ask({}, {})
        self.assertEqual(b.state, "closed")

    async def test_unknown_exceptions_surface(self):
        gw = JevGateway(FakeJev(fail_with=ZeroDivisionError), breaker=CircuitBreaker(5, 30), max_concurrency=2)
        with self.assertRaises(ZeroDivisionError): await gw.ask({}, {})


class ResilienceTests(unittest.IsolatedAsyncioTestCase):
    def test_breaker_cycle(self):
        clk = Clock(); b = CircuitBreaker(2, 30, clock=clk)
        b.record_failure(); b.record_failure(); self.assertEqual(b.state, "open")
        clk.t = 31; self.assertTrue(b.allow()); self.assertFalse(b.allow())
        b.record_failure(); self.assertEqual(b.state, "open")
        clk.t = 70; self.assertTrue(b.allow()); b.record_success(); self.assertEqual(b.state, "closed")

    async def test_single_flight(self):
        cache, n = SingleFlightCache(60, 10), []
        async def compute():
            n.append(1); await asyncio.sleep(0.01); return {"v": 1}, True
        r = await asyncio.gather(*(cache.get_or_compute("k", compute) for _ in range(5)))
        self.assertEqual((len(n), sum(1 for _, s in r if s)), (1, 4))


if __name__ == "__main__":
    unittest.main()
