"""Shared contract for a NiFi-side Python adapter; independent of Django.

Verify first, constrain the database query to claims['branches'] BEFORE grouping
or limiting, and attach authorization_receipt(claims) to the returned envelope.
Never accept a branch list or an identity supplied outside the signed token.
"""
import json
import re
import jwt

TOKEN_FIELD = 'loan_access_token'
ISSUER = 'buro-insight-ai'
AUDIENCE = 'buro-loan-query'
CODE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def verify_loan_access_token(token, key, tool, parameters):
    if not isinstance(key, str) or len(key.encode()) < 32:
        raise ValueError('A dedicated authorization signing key of at least 32 bytes is required.')
    claims = jwt.decode(token, key, algorithms=['HS256'], issuer=ISSUER, audience=AUDIENCE,
                        options={'require': ['exp', 'iat', 'nbf', 'sub', 'jti', 'tool', 'branches', 'query', 'scope_version']})
    if (type(claims.get('v')) is not int or claims['v'] != 1 or claims['tool'] != tool
            or not isinstance(claims['sub'], str) or not re.fullmatch(r'[1-9][0-9]{0,18}', claims['sub'])
            or any(type(claims[key]) is not int for key in ('iat', 'nbf', 'exp'))
            or claims['nbf'] != claims['iat'] or not 0 < claims['exp'] - claims['iat'] <= 180
            or not isinstance(claims['scope_version'], str) or not re.fullmatch(r'[0-9a-f]{64}', claims['scope_version'])
            or not isinstance(claims['jti'], str) or not re.fullmatch(r'[0-9a-f]{32}', claims['jti'])):
        raise ValueError('Invalid authorization context.')
    branches = claims['branches']
    if (not isinstance(branches, list) or not 1 <= len(branches) <= 5000
            or any(not isinstance(code, str) or not CODE.fullmatch(code) for code in branches)
            or branches != sorted(set(branches))):
        raise ValueError('Invalid authorized branches.')
    supplied = {k: v for k, v in parameters.items() if k != TOKEN_FIELD}
    if canonical(supplied) != canonical(claims['query']):
        raise ValueError('Query differs from its signed authorization.')
    return claims


def authorization_receipt(claims):
    return {'version': 1, 'request_id': claims['jti'], 'subject': claims['sub'],
            'scope_version': claims['scope_version'], 'branch_codes': claims['branches']}
