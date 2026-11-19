"""Save and read-only test of administrator-owned reporting connections."""
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import permission_required
from django.shortcuts import render, redirect
from django.views.decorators.csrf import csrf_protect

from .reporting_config import ReportingForm, initial, save, current
from .superset_publisher import SupersetClient, PublishError


def check_connection():
    config = current()
    if not config.get('URL') or not config.get('USERNAME') or not config.get('PASSWORD'):
        raise ValueError('Step 1 needs a Superset address and service credentials. Save them first.')
    client = SupersetClient(config)
    try:
        client.login()
        if not config.get('DATABASE_ID'):
            raise ValueError('Superset login works. Add the analytics database ID in step 2.')
        client.call('GET', f"database/{config['DATABASE_ID']}")
    finally:
        client.close()
    if not config.get('SNAPSHOT_DB'):
        raise ValueError('Superset login works. Complete the report storage fields in step 2.')
    import psycopg
    with psycopg.connect(**config['SNAPSHOT_DB']) as connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT current_database(), has_schema_privilege(current_user, %s, %s)',
                           (config.get('SCHEMA','agentic'),'CREATE'))
            name, permitted = cursor.fetchone()
            if name != config['SNAPSHOT_DB']['dbname'] or not permitted:
                raise ValueError('Storage connected, but the writer needs CREATE permission in the configured analytics schema.')
    return 'Superset login, registered database access and snapshot storage checks passed. User mappings and Superset access rules still apply.'


@staff_member_required(login_url='/login/')
@permission_required(('agentic_ai.add_agenttool','agentic_ai.change_agenttool','chat.change_systemsetting'),raise_exception=True)
@csrf_protect
def reporting_settings(request):
    if request.method not in {'GET','POST'}:
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(['GET','POST'])
    notice = 'Reporting settings saved. Use Test saved connection to verify them.' if request.GET.get('saved') else ''
    form = ReportingForm(request.POST if request.method == 'POST' and request.POST.get('action') == 'save' else None, initial=initial())
    if request.method == 'POST':
        if request.POST.get('action') == 'save' and form.is_valid():
            try:
                save(request.user,form.cleaned_data)
                return redirect(request.path+'?saved=1')
            except ValueError as exc:
                form.add_error(None,str(exc))
        elif request.POST.get('action') == 'check':
            try:
                notice = check_connection()
            except (ValueError,PublishError) as exc:
                notice = str(exc)
            except Exception:
                notice = 'The connection test could not complete. Check the saved addresses, credentials, TLS trust and network access.'
    sections = [
        ('01 / CONNECT SUPERSET','Connect the reporting service', ['service_url','public_url','allow_http','username','password']),
        ('02 / REPORT STORAGE','Connect the dedicated analytics database', ['database_id','database_host','database_port','database_name','database_user','database_password','database_sslmode','schema']),
        ('03 / USERS & TEMPLATE','Choose who publishes and the default layout', ['user_mappings','private_access_verified','default_template']),
        ('04 / MAP IMAGERY', 'Set the default map background for new dashboards',
         ['map_background', 'map_tile_url', 'map_tile_attribution']),
        ('05 / POWER BI REPORT SERVER', 'Configure the on-premises Power BI handoff',
         ['powerbi_mode', 'powerbi_portal_url', 'powerbi_allow_http'])]
    response = render(request,'agentic_ai/reporting_settings.html',{'form':form,'notice':notice,
        'sections':[(step,title,[form[name] for name in fields]) for step,title,fields in sections]})
    response['Cache-Control']='no-store'
    return response
