"""ho-odoo-access-check, the Odoo-specific half of `ho-odoo-security` (docs/design/child-projects.md)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CHECK = Path(__file__).resolve().parents[2] / "workers/toolchains/odoo/ho-odoo-access-check"
ACCESS = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"


def module(tmp_path: Path, models: str, access_models: list[str], controller: str = "") -> Path:
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "main.py").write_text(models)
    (tmp_path / "security").mkdir()
    (tmp_path / "security" / "ir.model.access.csv").write_text(
        ACCESS + "".join(f"access_{m},{m},{m},base.group_user,1,1,1,1\n" for m in access_models))
    if controller:
        (tmp_path / "controllers").mkdir()
        (tmp_path / "controllers" / "main.py").write_text(controller)
    return tmp_path


def run(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(CHECK), str(path)], capture_output=True, text=True, check=False)


MODELS = '''
from odoo import models


class Lease(models.Model):
    _name = "estate.lease"


class Wizard(models.TransientModel):
    _name = "estate.lease.wizard"


class Mixin(models.AbstractModel):
    _name = "estate.mixin"


class Partner(models.Model):
    _inherit = "res.partner"
'''


def test_every_stored_model_with_access_rules_passes(tmp_path):
    result = run(module(tmp_path, MODELS, ["model_estate_lease", "estate.model_estate_lease_wizard"]))
    assert result.returncode == 0, result.stdout


def test_a_model_without_access_rules_fails(tmp_path):
    result = run(module(tmp_path, MODELS, ["model_estate_lease"]))
    assert result.returncode == 1
    assert "models/main.py:10: model estate.lease.wizard has no line in security/ir.model.access.csv" in result.stdout
    assert "estate.mixin" not in result.stdout and "res.partner" not in result.stdout


def test_a_module_without_an_access_file_fails(tmp_path):
    path = module(tmp_path, MODELS, [])
    (path / "security" / "ir.model.access.csv").unlink()
    assert run(path).stdout.count("has no line") == 2


CONTROLLER = '''
from odoo import http


class Site(http.Controller):
    @http.route("/page", type="http", auth="public", website=True)
    def page(self):
        return ""

    @http.route("/hook", type="http", auth="public", csrf=False, methods=["POST"])
    def hook(self):
        return ""

    @http.route("/raw", type="http", auth="none")
    def raw(self):
        return ""

    @http.route("/signed", type="http", auth="public", csrf=False)  # nosec: the payload is signed
    def signed(self):
        return ""

    @http.route("/form", type="http", auth="user", csrf=False)
    def form(self):
        return ""
'''


def test_unauthenticated_routes_and_public_routes_without_csrf_fail(tmp_path):
    result = run(module(tmp_path, "", [], CONTROLLER))
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert lines == ["controllers/main.py:10: hook: a public route with csrf=False",
                     'controllers/main.py:14: raw: auth="none" runs without any user or database']


def test_tests_and_migrations_are_not_checked(tmp_path):
    path = module(tmp_path, "", [])
    for folder in ("tests", "migrations"):
        (path / folder).mkdir()
        (path / folder / "main.py").write_text(MODELS)
    assert run(path).returncode == 0
