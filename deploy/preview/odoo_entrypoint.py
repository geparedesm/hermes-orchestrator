"""Preview-only entry point: refuse to serve any non-anonymized database."""
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
if mode == 'sanitize':
    subprocess.run(command + ['neutralize', '-d', 'preview'], check=True)
    subprocess.run(command + ['shell', '-d', 'preview', '--no-http'],
                   input=Path('/preview/anonymize_odoo.py').read_text(), text=True, check=True)
elif mode == 'seed':
    subprocess.run(command + ['shell', '-d', 'preview', '--no-http'],
                   input=Path('/preview/seed_odoo.py').read_text(), text=True, check=True)
elif mode == 'upgrade':
    modules = sorted(p.name for p in Path('/opt/custom-addons').iterdir()
                     if p.is_dir() and (p / '__manifest__.py').is_file())
    connection = psycopg2.connect(host='db', user='preview', dbname='preview', password=options['db_password'])
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
    for name in modules:
        seed = Path('/opt/custom-addons') / name / '.hermes' / 'preview-seed.py'
        if seed.is_file():
            subprocess.run(command + ['shell', '-d', 'preview', '--no-http'], input=seed.read_text(), text=True, check=True)
elif mode == 'serve':
    connection = psycopg2.connect(host='db', user='preview', dbname='preview', password=options['db_password'])
    with connection, connection.cursor() as cursor:
        cursor.execute("SELECT value FROM ir_config_parameter WHERE key = 'hermes.preview.anonymized'")
        if cursor.fetchone() != ('v1',):
            raise RuntimeError('Preview refuses to serve a database without the anonymization marker')
    connection.close()
    os.execvp(command[0], command)
else:
    raise RuntimeError('unsupported preview operation')
