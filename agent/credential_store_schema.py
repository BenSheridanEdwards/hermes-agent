"""Shared, non-mutating validation of credential-owner metadata."""
from datetime import datetime
import json
import math


def invalid_store():
    from agent.credential_policy import CredentialPolicyError
    return CredentialPolicyError('Credential store is invalid or unreadable')


def validate_store(data):
    def require(condition):
        if not condition:
            raise invalid_store()

    def credentials(row):
        for field in ('access_token', 'refresh_token'):
            require(row.get(field) is None or isinstance(row[field], str))
        for field in ('expires_at_ms', 'expires_at'):
            value = row.get(field)
            if value is None:
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                require(math.isfinite(value))
            elif field == 'expires_at' and isinstance(value, str):
                try:
                    datetime.fromisoformat(value.replace('Z', '+00:00'))
                except ValueError:
                    raise invalid_store() from None
            else:
                raise invalid_store()

    require(isinstance(data, dict))
    if 'providers' in data:
        require(isinstance(data['providers'], dict))
        for state in data['providers'].values():
            require(isinstance(state, dict))
            if 'tokens' in state:
                require(isinstance(state['tokens'], dict))
                credentials(state['tokens'])
    if 'credential_pool' in data:
        require(isinstance(data['credential_pool'], dict))
        for rows in data['credential_pool'].values():
            require(isinstance(rows, list))
            ids = set()
            for row in rows:
                require(isinstance(row, dict))
                cid = row.get('id')
                require(isinstance(cid, str) and bool(cid.strip()) and cid not in ids)
                ids.add(cid)
                credentials(row)
    return data


def read_store(path):
    """None is missing, never present-unreadable. No backup or other write."""
    try:
        text = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        raise invalid_store() from None
    try:
        data = json.loads(text)
    except (ValueError, UnicodeError):
        raise invalid_store() from None
    return validate_store(data)
