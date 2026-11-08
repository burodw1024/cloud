"""Create a disabled, local review draft from a NiFi 2.4 flow export.

Only the connected QueryLoanDue 1.1.0 path is changed. Never contacts NiFi,
updates a live group, embeds a signing key, or rewrites unrelated services.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import uuid

HERE = Path(__file__).resolve().parent


def prepare(document):
    result = deepcopy(document)
    group = result['flowContents']
    processors = {p['identifier']: p for p in group['processors']}
    connections = group['connections']
    candidates = [p for p in processors.values() if p['type'] == 'com.example.nifi.QueryLoanDue'
                  and p['bundle']['version'] == '1.1.0']
    if len(candidates) != 1:
        raise ValueError('Expected exactly one QueryLoanDue 1.1.0 processor.')
    query = candidates[0]
    qid = query['identifier']
    incoming = [c for c in connections if c['destination']['id'] == qid and 'success' in c['selectedRelationships']]
    outgoing = [c for c in connections if c['source']['id'] == qid and 'success' in c['selectedRelationships']]
    failed = [c for c in connections if c['source']['id'] == qid and 'failure' in c['selectedRelationships']]
    if len(incoming) != 1 or len(outgoing) != 1 or len(failed) != 1:
        raise ValueError('Unrecognized loan query relationships; review manually.')
    verifier = processors[incoming[0]['source']['id']]
    response = processors[outgoing[0]['destination']['id']]
    request_links = [c for c in connections if c['destination']['id'] == verifier['identifier'] and 'success' in c['selectedRelationships']]
    if len(request_links) != 1:
        raise ValueError('Expected one HTTP request path into the loan scope step.')
    request = processors[request_links[0]['source']['id']]
    if not (verifier['type'].endswith('.UpdateAttribute') and request['type'].endswith('.HandleHttpRequest')
            and response['type'].endswith('.HandleHttpResponse')):
        raise ValueError('Unexpected processor types; no draft was written.')
    if any(c['source']['id'] != qid for c in connections if c['destination']['id'] == response['identifier']):
        raise ValueError('HTTP response processor is shared; isolate the loan response before generating a draft.')
    if query['properties'].get('Authorized Branches JSON') != '${auth.branch_codes}':
        raise ValueError('QueryLoanDue must read the verified auth.branch_codes attribute.')
    if request['properties'].get('HTTP Context Map') != response['properties'].get('HTTP Context Map'):
        raise ValueError('HTTP request and response use different context maps.')

    changed = [request['identifier'], verifier['identifier'], qid, response['identifier']]
    template = deepcopy(verifier)

    def script_processor(target, name, script, tool=False):
        target.update(name=name, type='org.apache.nifi.processors.script.ExecuteScript',
                      bundle={'group': 'org.apache.nifi', 'artifact': 'nifi-scripting-nar', 'version': '2.4.0'},
                      propertyDescriptors={}, autoTerminatedRelationships=[], scheduledState='DISABLED',
                      retryCount=0, retriedRelationships=[])
        target['properties'] = {'Script Engine': 'Groovy', 'Script File': None,
                                'Script Body': (HERE/script).read_text(), 'Module Directory': None}
        if tool:
            target['properties']['expectedTool'] = 'loan-due-report'
            target['propertyDescriptors']['expectedTool'] = {'name': 'expectedTool', 'displayName': 'expectedTool',
                'sensitive': False, 'dynamic': True, 'identifiesControllerService': False}
        target['comments'] = 'LOCAL REVIEW DRAFT: configure shared signing key in NiFi environment. Validate custom SQL scope before activation.'

    script_processor(verifier, 'Verify user loan access', 'verify-loan-access.groovy', tool=True)
    def new_processor(label, name, script):
        target = deepcopy(template)
        identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, qid + '/' + label))
        target.update(identifier=identifier, instanceIdentifier=identifier)
        target['position'] = {'x': query['position']['x'] + 400, 'y': query['position']['y'] + (250 if label == 'receipt' else 500)}
        script_processor(target, name, script)
        group['processors'].append(target)
        return target
    receipt = new_processor('receipt', 'Attach verified loan receipt', 'attach-loan-receipt.groovy')
    error = new_processor('backend-error', 'Return loan backend error', 'backend-error.groovy')

    def endpoint(processor):
        return {'id': processor['identifier'], 'instanceIdentifier': processor['instanceIdentifier'],
                'type': 'PROCESSOR', 'groupId': processor['groupIdentifier'], 'name': processor['name'], 'comments': ''}
    edge_template = deepcopy(outgoing[0])
    outgoing[0]['destination'] = endpoint(receipt)
    failed[0]['destination'] = endpoint(error)
    def add_edge(source, relationship, destination):
        edge = deepcopy(edge_template)
        identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, source['identifier']+'/'+relationship+'/'+destination['identifier']))
        edge.update(identifier=identifier, instanceIdentifier=identifier, name='', bends=[],
                    source=endpoint(source), destination=endpoint(destination), selectedRelationships=[relationship])
        connections.append(edge)
    add_edge(verifier, 'failure', response)
    add_edge(receipt, 'success', response)
    add_edge(receipt, 'failure', response)
    add_edge(error, 'success', response)
    for edge in connections:
        for side in ('source', 'destination'):
            if edge[side]['id'] == verifier['identifier']:
                edge[side] = endpoint(verifier)
    response['properties']['HTTP Status Code'] = '${http.status.code}'
    request['properties'].update({'Allow POST': 'true', 'Allow GET': 'false', 'Allow HEAD': 'false',
                                 'Allowed Paths': '/api/loan-due/query'})
    for identifier in changed:
        processors[identifier]['scheduledState'] = 'DISABLED'
    return result, {'changed_processors': changed, 'new_processors': [receipt['identifier'], error['identifier']],
                    'loan_path_disabled': True, 'signing_key_embedded': False,
                    'savings_changed': False, 'sql_enforcement_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.input.resolve() == args.output.resolve():
        raise SystemExit('Output must be a new file; the original export is never overwritten.')
    result, summary = prepare(json.loads(args.input.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
