"""Idempotent synthetic acceptance scenarios, exclusively for the preview database."""
# ruff: noqa: F821 -- env is supplied by the Odoo shell
import base64
from datetime import date, timedelta
from io import BytesIO
from pypdf import PdfWriter

if env.cr.dbname != 'preview':
    raise RuntimeError('Synthetic scenarios belong only in the preview database')
if 'commercial_properties.property' in env:
    building = env['commercial_properties.property'].search([('name', '=', 'Example building')], limit=1)
    if not building:
        building = env['commercial_properties.property'].create({'name': 'Example building', 'address': '100 Example Street'})
    unit = env['commercial_properties.unit'].search([('name', '=', 'Example office'), ('property_id', '=', building.id)], limit=1)
    if not unit:
        unit = env['commercial_properties.unit'].create({'name': 'Example office', 'property_id': building.id,
                                                        'unit_type': 'office', 'monthly_rent': 1500})
    tenant = env['commercial_properties.tenant'].search([('identity_card', '=', 'TEST-ID-001')], limit=1)
    if not tenant:
        tenant = env['commercial_properties.tenant'].create({'name': 'Example tenant', 'identity_card': 'TEST-ID-001',
                                                            'email': 'tenant@example.invalid'})
    template = env['commercial_properties.contract.template'].search([('name', '=', 'Example rental template')], limit=1)
    if not template:
        template = env['commercial_properties.contract.template'].create({'name': 'Example rental template',
                      'content': '<h1>Example rental contract</h1><p>Synthetic data for acceptance testing.</p>'})
    if not env['commercial_properties.contract'].search_count([('unit_id', '=', unit.id)]):
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.add_metadata({'/Title': 'Synthetic preview contract'})
        output = BytesIO()
        writer.write(output)
        env['commercial_properties.contract'].create({'tenant_id': tenant.id, 'unit_id': unit.id,
            'template_id': template.id, 'start_date': date.today() - timedelta(days=30),
            'end_date': date.today() + timedelta(days=60), 'monthly_rent': 1500, 'state': 'active',
            'scanned_document': base64.b64encode(output.getvalue()), 'scanned_document_filename': 'synthetic-contract.pdf'})
env.cr.commit()
print('Synthetic rental contract and its payment scenarios are available.')
