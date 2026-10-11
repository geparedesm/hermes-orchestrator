from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestInstall(TransactionCase):
    def test_module_is_installed(self):
        module = self.env["ir.module.module"].search([("name", "=", "ho_sample")])
        self.assertEqual(module.state, "installed")
