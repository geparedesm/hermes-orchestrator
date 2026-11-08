from control_plane.gate_context import contract
from ho_core.verification import plan_verification


def test_contract_tracks_the_effective_planner_and_commands():
    config = {'commands': {'test': 'ho-odoo-test', 'lint': 'ruff check .', 'typecheck': 'mypy .', 'security': 'pip-audit'},
              'quality_gate': {'docs_updated': True, 'block_on_findings': ['MEDIUM']}}
    paths = ['models/payments.py', 'tests/test_payments.py']
    text = contract(config, paths)
    for requirement in plan_verification(config, paths).requirements:
        assert requirement.name in text
    assert 'ho-odoo-test' in text and 'mypy .' in text
    assert 'CRITICAL, HIGH.' in text and 'MEDIUM and LOW findings are follow-ups' in text
    assert 'docs_updated' in text and 'actual integrated diff' in text


def test_contract_explains_unknown_scope_risk_and_unavailable_commands():
    text = contract({'commands': {}, 'quality_gate': {}}, [])
    assert 'build, tests, lint, typecheck and security' in text
    assert 'Do not edit .hermes/project.yaml' in text and 'sets the project up' not in text
    setup = contract({'commands': {}, 'quality_gate': {}}, [], setup=True)  # a template project's setup task
    assert 'no build, test, lint, typecheck, security command yet' in setup and 'sets the project up' in setup
    assert 'test gaps' in text and 'human exception' in text
    assert 'CI checks' not in text and 'other provider' in text
    assert 'approval bypasses' in text
