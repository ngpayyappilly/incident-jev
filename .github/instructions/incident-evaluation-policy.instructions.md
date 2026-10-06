---
name: Incident Evaluation Policy Rules
description: "Use when changing incident-evaluation probes, Jev questions, signals, confidence handling, fallback findings, or deterministic verdict composition."
applyTo: ["app/probes.py", "app/decide.py", "app/signals.py"]
---

# Incident Evaluation Policy Rules

- Keep `probes.py`, `decide.py`, and `signals.py` deterministic, side-effect free, and free of SDK imports.
- Treat Jev as a narrow typed-question service. Questions must be atomic, answerable solely from paths within that question's own probe-group state with no access to other groups' state, and named with the `{group}__{question}` convention using `signals.SEP`.
- Preserve question-state paths: every path referenced by a question must exist in that probe group's state. If a referenced path does not exist, add it to that group's state or update the question in the same change; never leave a question referencing a missing path. Grouped questions must remain together when packed within the configured maximum number of questions per Jev request.
- Preserve the two-stage contract: stage 1 asks base questions; stage 2 contains only conditional follow-ups justified by stage 1 answers.
- Keep `NoulSig` and `ChoiceSig` confidence semantics explicit. When a Jev answer is uncertain, low-confidence, or malformed, exclude it from verdict inputs, fall back to the rule-based or regex path in `decide.py`, and record the fallback reason.
- Keep rule-based checks and regex fallbacks in `decide.py` useful when Jev is unavailable. Do not make model availability a prerequisite for core claim, routing, approval, or lifecycle findings.
- When changing a signal, question, threshold, or weighted decision rule, update the focused tests in `tests/test_pipeline.py` and cover both positive and uncertain/degraded outcomes.
- Do not tune wording, thresholds, tool mappings, or composite weights solely to satisfy an existing fixture; establish the intended incident policy and add a regression case.