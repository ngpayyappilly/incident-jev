---
name: Incident Evaluation Policy Rules
description: "Use when changing incident-evaluation probes, Jev questions, signals, confidence handling, fallback findings, or deterministic verdict composition."
applyTo: ["app/probes.py", "app/decide.py", "app/signals.py"]
---

# Incident Evaluation Policy Rules

- Keep `probes.py`, `decide.py`, and `signals.py` deterministic, side-effect free, and free of SDK imports.
- Treat Jev as a narrow typed-question service. Questions must be atomic, answerable from their supplied state, and named with the `{group}__{question}` convention using `signals.SEP`.
- Preserve question-state paths: every path referenced by a question must exist in that probe group's state, and grouped questions must remain together when packed under the request budget.
- Preserve the two-stage contract: stage 1 asks base questions; stage 2 contains only conditional follow-ups justified by stage 1 answers.
- Keep `NoulSig` and `ChoiceSig` confidence semantics explicit. Uncertain or low-confidence answers must not silently become definitive findings or verdict inputs.
- Keep rule-based checks and regex fallbacks in `decide.py` useful when Jev is unavailable. Do not make model availability a prerequisite for core claim, routing, approval, or lifecycle findings.
- When changing a signal, question, threshold, or weighted decision rule, update the focused tests in `tests/test_pipeline.py` and cover both positive and uncertain/degraded outcomes.
- Do not tune wording, thresholds, tool mappings, or composite weights solely to satisfy an existing fixture; establish the intended incident policy and add a regression case.