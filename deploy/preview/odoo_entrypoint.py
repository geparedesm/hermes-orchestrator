"""Preview-only entry point: serve only a database that is synthetic or anonymized, never a raw production copy."""
import configparser
import os
from pathlib import Path
import subprocess
import sys
import psycopg2

os.umask(0o077)
config = configparser.ConfigParser(interpolation=None)
config.read('/etc/odoo/odoo.conf')
options = config['options']
options.update({'db_host': 'db', 'db_port': '5432', 'db_user': 'preview', 'db_name': 'preview', 'dbfilter': '^preview$',
                'db_password': Path('/run/secrets/preview_db_password').read_text().strip(),
                'admin_passwd': Path('/run/secrets/preview_master_password').read_text().strip(),
                'workers': '1', 'max_cron_threads': '0', 'list_db': 'False', 'http_interface': '0.0.0.0',
                'http_port': '8069', 'proxy_mode': 'True', 'db_sslmode': 'disable', 'log_level': 'warn',
                'smtp_server': '127.0.0.1', 'smtp_port': '1'})
with open('/tmp/preview.conf', 'w') as output:
    config.write(output)
os.environ['ODOO_RC'] = '/tmp/preview.conf'
command = ['python3', '/usr/bin/odoo']
mode = sys.argv[1] if len(sys.argv) > 1 else 'serve'

# Run in the Odoo shell of a database this entry point has just created: only generated values, never a copied record.
SYNTHETIC_SETUP = """
from pathlib import Path
import uuid
if env.cr.dbname != 'preview':
    raise RuntimeError('Synthetic setup belongs only in the preview database')
env['ir.cron'].search([]).write({'active': False})
env.ref('base.user_admin').write({'login': 'admin', 'active': True,
                                   'password': Path('/run/secrets/preview_admin_password').read_text().strip()})
Parameters = env['ir.config_parameter'].sudo()
Parameters.set_param('database.secret', uuid.uuid4().hex + uuid.uuid4().hex)
Parameters.set_param('web.base.url', 'https://test.gp-developer.com')
Parameters.set_param('web.base.url.freeze', 'True')
Parameters.set_param('report.url', 'http://127.0.0.1:8069')
Parameters.set_param('hermes.preview.synthetic', 'v1')
env.cr.commit()
print('Synthetic preview database ready; it holds no production data.')
"""


def connect():
    return psycopg2.connect(host='db', user='preview', dbname='preview', password=options['db_password'])


def custom_modules():
    return sorted(p.name for p in Path('/opt/custom-addons').iterdir() if p.is_dir() and (p / '__manifest__.py').is_file())


def shell(script):
    subprocess.run(command + ['shell', '-d', 'preview', '--no-http'], input=script, text=True, check=True)


def module_seeds(modules):
    for name in modules:
        seed = Path('/opt/custom-addons') / name / '.hermes' / 'preview-seed.py'
        if seed.is_file():
            shell(seed.read_text())


if mode == 'init-synthetic':
    # The baseline for an installation whose production data has no reviewed anonymization policy: a new database with
    # the custom modules and generated scenarios only. Refuses a database that already holds anything.
    connection = connect()
    with connection, connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.ir_module_module') IS NOT NULL")
        if cursor.fetchone()[0]:
            raise RuntimeError('The preview database is already initialized; remove the preview-db volume to start over')
    connection.close()
    modules = custom_modules()
    subprocess.run(command + ['-d', 'preview', '-i', ','.join(modules), '--stop-after-init', '--no-http'], check=True)
    shell(SYNTHETIC_SETUP)
    shell(Path('/preview/seed_odoo.py').read_text())
    module_seeds(modules)
elif mode == 'sanitize':
    subprocess.run(command + ['neutralize', '-d', 'preview'], check=True)
    shell(Path('/preview/anonymize_odoo.py').read_text())
elif mode == 'seed':
    shell(Path('/preview/seed_odoo.py').read_text())
elif mode == 'upgrade':
    modules = custom_modules()
    connection = connect()
    with connection, connection.cursor() as cursor:
        cursor.execute("SELECT name FROM ir_module_module WHERE state='installed'")
        installed = {row[0] for row in cursor.fetchall()}
    connection.close()
    args = []
    new = [name for name in modules if name not in installed]
    updates = [name for name in modules if name in installed]
    if new:
        args += ['-i', ','.join(new)]
    if updates:
        args += ['-u', ','.join(updates)]
    subprocess.run(command + ['-d', 'preview', *args, '--stop-after-init', '--no-http'], check=True)
    module_seeds(modules)
elif mode == 'serve':
    connection = connect()
    with connection, connection.cursor() as cursor:
        cursor.execute("SELECT key, value FROM ir_config_parameter "
                       "WHERE key IN ('hermes.preview.anonymized', 'hermes.preview.synthetic')")
        markers = dict(cursor.fetchall())
    connection.close()
    if 'v1' not in markers.values():
        raise RuntimeError('Preview refuses to serve a database that is neither synthetic nor anonymized')
    os.execvp(command[0], command)
else:
    raise RuntimeError('unsupported preview operation')
