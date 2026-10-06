# Incident evaluation: AI-powered software on TypeSafe Jev

Code owns the workflow; Jev only answers narrow, typed questions.

    trace -> facts.py (code) -> probes.py (questions) -> judge.py (Jev, parallel)
          -> decide.py (code: verify, compose, route) -> verdict

    pip install -r requirements.txt
    export TYPESAFE_API_KEY=... EVAL_API_KEY=... JEV_MODEL=<pinned version>
    uvicorn app.main:app --port 8080
    curl -s localhost:8080/v1/evaluations -H "X-API-Key: $EVAL_API_KEY" \
         -H 'content-type: application/json' -d @tests/fixtures/trace.json

Tests (stdlib only, fake client): `python -m unittest discover -s tests -t .`

Adapt before production: tool names (`ToolMap` in app/config.py), responder detection
(`RESPONDER_SUFFIXES`), the question wording in app/probes.py (tune in the TypeSafe Playground),
and the composite weights in app/decide.py (tune on labeled incidents).
