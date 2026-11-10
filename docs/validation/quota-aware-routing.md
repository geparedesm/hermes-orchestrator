# Quota-aware developer routing validation

Date: 2026-11-10. Design: [quota-aware routing](../design/quota-aware-routing.md).

Validation uses Python 3.12, real throwaway PostgreSQL and Redis from `compose.test.yaml`, the in-process Git
Service, and the fake Agent Manager. The live stack, its provider identities/credentials, and the main checkout
are not modified. No paid provider executions, worker-image rebuild, or deployment is required for these tests.

The existing development virtual environment was reused with `PYTHONPATH` pointing at this worktree's package and
service sources and `HO_SCHEMA_DIR` pointing at this worktree's schemas. Explicit temporary test directories avoid
unrelated cleanup of older pytest runs. With those environment variables set, the validation commands are:

```bash
python -m pytest tests/unit -q --basetemp=/tmp/ho-quota-release-unit
HO_TEST_DATABASE_URL=postgresql://ho_test_admin:ho_test_admin@127.0.0.1:55432/postgres \
HO_TEST_REDIS_URL=redis://127.0.0.1:56379/15 \
python -m pytest tests/integration/test_orchestration.py tests/integration/test_agents.py \
  tests/integration/test_runtime_and_orphans.py -q -p no:warnings --basetemp=/tmp/ho-quota-release-validation
ruff check packages services tests migrations scripts
python scripts/validate_schemas.py
git diff --check
```

Final results: **510 unit tests passed**, **124 integration tests passed**, Ruff passed, schema/example/machine-profile
validation passed, and `git diff --check` passed. Unit tests emit one Starlette TestClient dependency deprecation warning.

Behavioral coverage includes both quota winners, the restrictive weekly window, freshness boundaries and invalid
observations, missing/reset/stale observations, identity isolation, shadow mode and rollback, queued re-selection
without old provider model/session/image fields, duplicate-poll suppression, fix ownership, independent review,
existing lease/budget/recovery rules, and waiting without spending a launch when both quotas are exhausted.

Real provider observation completeness, model-specific pools, and consumption from other clients are not established
by fake-worker tests. The router deliberately falls back when its stored session/week observations are not recent
and complete. See the design's rollout and measurement limits before relying on enforcement for a particular model.
