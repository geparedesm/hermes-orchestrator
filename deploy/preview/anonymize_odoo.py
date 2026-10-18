"""Executed exclusively by Odoo shell against the isolated database named preview.

The policy deliberately supports the modules inventoried in this installation. Unknown installed modules
or unexpected sensitive fields block publication until their policy has been reviewed.
"""
from pathlib import Path
# ruff: noqa: F821 -- env is supplied by the Odoo shell
import uuid
import base64
import hashlib
import json

if env.cr.dbname != 'preview':
    raise RuntimeError('Anonymization is permitted only in the isolated preview database')
allowed_modules = {'api_doc', 'auth_passkey', 'auth_totp', 'base', 'base_import', 'base_import_module', 'base_setup',
                   'bus', 'commercial_properties', 'html_editor', 'iap', 'job_finder', 'rpc', 'web', 'web_tour', 'web_unsplash'}
installed = set(env['ir.module.module'].search([('state', '=', 'installed')]).mapped('name'))
if installed - allowed_modules:
    raise RuntimeError('Additional installed modules need an anonymization policy before publication')

# A production schema change requires a reviewed inventory before another source-data refresh.
policy = json.loads(Path('/preview/anonymization-policy.json').read_text())
shape = sorted([[f.model, f.name, f.ttype] for f in env['ir.model.fields'].search([])])
digest = hashlib.sha256(json.dumps(shape, separators=(',', ':')).encode()).hexdigest()
if digest != policy['schema_sha256']:
    raise RuntimeError('The source schema changed; review the anonymization inventory before publishing another copy')

# External connections are disabled by Odoo neutralize and by Docker's internal application network.
env['ir.cron'].search([]).write({'active': False})
# Uploaded files (CVs, identities, contracts, receipts), generated documents and resource caches are not copied.
# Module assets will be regenerated in this new filestore.
env['ir.attachment'].search([]).unlink()
for model in ('auth.passkey.key', 'auth_totp.device', 'res.users.apikeys'):
    if model in env:
        env[model].search([]).unlink()

# Replace required document fields with a generated one-page PDF, never an original uploaded document.
stream = b'BT /F1 16 Tf 50 750 Td (SYNTHETIC PREVIEW DOCUMENT - Alex Example) Tj ET'
objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
           b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
           b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
           b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream']
pdf = bytearray(b'%PDF-1.4\n')
offsets = [0]
for index, value in enumerate(objects, 1):
    offsets.append(len(pdf))
    pdf.extend(str(index).encode() + b' 0 obj\n' + value + b'\nendobj\n')
xref = len(pdf)
pdf.extend(b'xref\n0 6\n0000000000 65535 f \n')
for offset in offsets[1:]:
    pdf.extend(f'{offset:010d} 00000 n \n'.encode())
pdf.extend(b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n' + str(xref).encode() + b'\n%%EOF\n')
synthetic_document = base64.b64encode(pdf)

business_models = [name for name in env.registry.models
                   if name.startswith('commercial_properties.') or name == 'job.profile']
for model in business_models:
    Model = env[model].with_context(active_test=False)
    for record in Model.search([]):
        values = {}
        for name, field in Model._fields.items():
            if not field.store or field.compute or field.related or name in ('id', 'display_name'):
                continue
            if field.type == 'binary':
                values[name] = synthetic_document
            elif field.type in ('char', 'text', 'html'):
                if name == 'email':
                    values[name] = f'test-{record.id}@example.invalid'
                elif name == 'phone':
                    values[name] = f'+610000{record.id:06d}'
                elif name in ('website', 'github', 'linkedin'):
                    values[name] = f'https://example.invalid/test-{record.id}'
                elif name.endswith('filename'):
                    values[name] = 'synthetic-preview-document.pdf'
                elif name == 'content' and field.type == 'html':
                    values[name] = '<p>Test contract for this preview.</p>'
                else:
                    values[name] = f'Test {name.replace("_", " ")} {record.id}'
        if values:
            record.write(values)

# Preserve partner relationships, while removing identifying names, contact data, commercial notes and images.
Partner = env['res.partner'].with_context(active_test=False)
for partner in Partner.search([]):
    values = {'name': f'Test contact {partner.id}', 'email': f'contact-{partner.id}@example.invalid'}
    for name in ('phone', 'mobile', 'street', 'street2', 'zip', 'city', 'website', 'vat', 'company_registry', 'ref', 'comment',
                 'function', 'barcode', 'additional_info', 'image_1920'):
        if name in Partner._fields and Partner._fields[name].store and not Partner._fields[name].compute:
            values[name] = False
    partner.write(values)
for company in env['res.company'].search([]):
    company.write({'name': f'Test company {company.id}'})

# Remove transient imports, password-change wizards and enrollment material.
for name, Model in list(env.registry.models.items()):
    if getattr(Model, '_transient', False) and getattr(Model, '_auto', False):
        env[name].search([]).unlink()
if 'ir.mail_server' in env:
    servers = env['ir.mail_server'].search([])
    if servers:
        servers.write({'smtp_host': '127.0.0.1', 'smtp_port': 1, 'smtp_user': False, 'smtp_pass': False})
if 'iap.account' in env:
    for account in env['iap.account'].search([]):
        account.write({'account_token': uuid.uuid4().hex})

# Replace all human login credentials, then provision one preview-only operator credential from a secret reference.
Users = env['res.users'].with_context(active_test=False)
for user in Users.search([]):
    updates = {'login': f'preview-user-{user.id}@example.invalid', 'password': uuid.uuid4().hex + uuid.uuid4().hex}
    for field in ('totp_secret', 'oauth_uid', 'oauth_access_token', 'api_key'):
        if field in Users._fields and Users._fields[field].store:
            updates[field] = False
    if user.id not in (env.ref('base.user_admin').id, env.ref('base.user_root').id):
        updates['active'] = False
    user.write(updates)
admin = env.ref('base.user_admin')
admin.write({'login': 'admin', 'active': True,
             'password': Path('/run/secrets/preview_admin_password').read_text().strip()})

# Parameters not covered by an explicit technical allowlist may contain provider credentials or personal settings.
allowed_parameters = {'database.create_date', 'database.secret', 'database.uuid', 'web.base.url', 'web.base.url.freeze',
                      'base_setup.default_external_email_server', 'database.is_neutralized', 'report.url',
                      'base.login_cooldown_after', 'base.login_cooldown_duration'}
Parameters = env['ir.config_parameter'].sudo()
Parameters.search([('key', 'not in', list(allowed_parameters))]).unlink()
Parameters.set_param('database.secret', uuid.uuid4().hex + uuid.uuid4().hex)
Parameters.set_param('database.uuid', str(uuid.uuid4()))
Parameters.set_param('web.base.url', 'https://test.gp-developer.com')
Parameters.set_param('web.base.url.freeze', 'True')
Parameters.set_param('report.url', 'http://127.0.0.1:8069')
Parameters.set_param('database.is_neutralized', 'True')
# Imported logs and previous browser sessions must not carry production identifiers into the test interface.
env['ir.logging'].search([]).unlink()
if 'bus.bus' in env:
    env['bus.bus'].search([]).unlink()

# Add an entirely synthetic CV profile without restoring any uploaded production file.
if 'job.profile' in env:
    profile = env['job.profile'].search([('user_id', '=', admin.id)], limit=1)
    sample = {'user_id': admin.id, 'full_name': 'Alex Example', 'email': 'alex@example.invalid',
              'headline': 'Software Developer', 'skills': 'Python, Odoo, PostgreSQL',
              'summary': 'Synthetic profile prepared for preview acceptance tests.',
              'experience': 'Built example business applications.', 'education': 'Example software engineering course'}
    if profile:
        profile.write(sample)
    else:
        env['job.profile'].create(sample)
if 'commercial_properties.property' in env:
    building = env['commercial_properties.property'].create({'name': 'Example building', 'address': '100 Example Street'})
    env['commercial_properties.unit'].create({'name': 'Example office', 'property_id': building.id,
                                             'unit_type': 'office', 'monthly_rent': 1500,
                                             'electricity_meter_number': 'TEST-METER-001'})
    env['commercial_properties.tenant'].create({'name': 'Example tenant', 'identity_card': 'TEST-ID-001',
                                               'email': 'tenant@example.invalid', 'phone': '+61000000001'})
Parameters.set_param('hermes.preview.anonymized', 'v1')
env.cr.commit()
print('Preview anonymization and synthetic profile completed; source database was not modified.')
