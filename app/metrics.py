from prometheus_client import Counter, Histogram

_EVALS = Counter("incident_eval_total", "Evaluations produced", ["status", "priority"])
_JEV = Counter("incident_eval_jev_requests_total", "Jev requests by outcome", ["outcome"])
_JEV_LAT = Histogram("incident_eval_jev_latency_seconds", "Jev request latency",
                     buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
_REASONS = Counter("incident_eval_review_reasons_total", "Why evaluations were flagged", ["reason"])
_QUESTIONS = Counter("incident_eval_questions_total", "Questions sent to Jev")


class PrometheusMetrics:
    def jev(self, outcome: str, seconds: float) -> None:
        _JEV.labels(outcome).inc()
        if outcome == "ok":
            _JEV_LAT.observe(seconds)

    def evaluation(self, status: str, priority: str) -> None:
        _EVALS.labels(status, priority).inc()

    def reasons(self, reasons: list) -> None:
        for r in reasons:
            _REASONS.labels(r).inc()

    def questions(self, n: int) -> None:
        _QUESTIONS.inc(n)
