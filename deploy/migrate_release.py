"""Executed once with all old application pods stopped; no automatic retry."""
import json
import os
from pathlib import Path
import sys

# Absolute-file execution starts in /app/deploy, not the Django project root.
APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))
os.chdir(APP_ROOT)
import sqlite3
import subprocess

root=Path('/data')
source=root/'db.sqlite3'
release=os.environ['AGENT_RELEASE_ID']
assert source.is_file(), 'Existing production database missing; refusing first-install behavior.'
backups=root/'deployment-backups'/release
backups.mkdir(parents=True,exist_ok=False,mode=0o700)
for name in ('db.sqlite3','cache.sqlite3'):
    path=root/name
    if path.exists():
        with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=30) as src:
            with sqlite3.connect(str(backups/name)) as dst:
                src.backup(dst)
                assert dst.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        os.chmod(backups/name,0o600)
print('SQLite database and existing shared cache backed up:',backups,flush=True)
os.environ.setdefault('DJANGO_SETTINGS_MODULE','buro_first.settings_production')
subprocess.run(['python','manage.py','migrate','--noinput'],check=True)
import django
django.setup()
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from chat.models import SystemSetting
from chat.dynamic_config import clear_setting_cache
assert settings.POLICY_WORK_DIR=='/data/policy_rag'
assert settings.DATABASES['default']['ENGINE']=='django.db.backends.sqlite3'
assert settings.CACHES['default']['BACKEND']=='agentic_ai.sqlite_cache.SQLiteCache'
assert get_user_model().objects.filter(username='root_123',is_active=True,is_staff=True).exists(), 'Existing root_123 administrator not found.'
updates={'MCP_GATEWAY_URL':'http://192.168.1.99:30444/servers/3af4c69246314595a30043f0b8d8b654/mcp',
         'MCP_TIMEOUT':'1500','MCP_REQUEST_TIMEOUT':'1800','POLICY_WORK_DIR':'/data/policy_rag',
         'OLLAMA_URL':'http://192.168.1.144:31134'}
with transaction.atomic():
    for key,value in updates.items():
        SystemSetting.objects.update_or_create(key=key,defaults={'value':value,'is_active':True})
clear_setting_cache()
(root/'policy_rag').mkdir(exist_ok=True)
# User accounts, role registrations, approval flags, selected model and tokens stay unchanged.
from agentic_ai.mcp_gateway import gateway_enabled,load_config,discover_gateway,probe_model
from asgiref.sync import async_to_sync
assert gateway_enabled(), 'Expected the existing MCP gateway to be enabled; configuration was preserved.'
config=load_config()
assert config['url']==updates['MCP_GATEWAY_URL']
assert config['timeout']==1500 and config['request_timeout']==1800
assert config['token'], 'Existing gateway token is missing; no replacement token was generated.'
tools=async_to_sync(discover_gateway)(config)
assert any(tool.name=='loan-due-report' for tool in tools), 'Server-scoped loan-due-report tool not found.'
# Synthetic model probe only: NEVER call the real loan report as a deployment test.
probe=async_to_sync(probe_model)(config)
assert probe['gateway_tool_executed'] is False
from agentic_ai.report_presentation import present_report
from agentic_ai.mcp_gateway import _model_response
presentation=async_to_sync(present_report)(config,
    'Show only outstanding principal from this already scoped sample report.',
    {'structuredContent':{'rows':[{'OUTSTANDINGP':'123.45','DUEAMOUNT':'7.00'}]}}, _model_response)
assert presentation['raw']['structuredContent']['columns']==['OUTSTANDINGP'], 'Selected model failed the synthetic report-column selection probe.'
print('MCP discovery, project-model selection and synthetic report formatting passed; no real report was executed.',flush=True)
subprocess.run(['python','manage.py','check','--deploy','--fail-level','ERROR'],check=True)
(backups/'migration-complete.json').write_text(json.dumps({'release':release,'model_probe_passed':True}))
print('Migration completed; existing users, approvals and credentials preserved.',flush=True)
