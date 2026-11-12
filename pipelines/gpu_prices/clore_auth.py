"""Optional auth per https://docs.clore.ai/for-hosts/api; safe diagnostics."""
import os
from urllib.error import HTTPError
from urllib.parse import urlsplit

API = 'https://api.clore.ai/v1/marketplace'


def auth_headers(url):
    parts = urlsplit(url)
    if not (parts.scheme == 'https' and parts.hostname == 'api.clore.ai'
            and parts.port in (None, 443) and parts.path == '/v1/marketplace'
            and not parts.username and not parts.password and not parts.fragment):
        return {}
    key = os.environ.get('CLORE_API_KEY', '').strip()
    if not key:
        return {}
    if any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise CloreRequestError(0, True)
    return {'auth': key}


class CloreRequestError(HTTPError):
    """Never retain remote bodies, headers or exception messages in errors."""
    def __init__(self, status, has_key):
        message = ('set CLORE_API_KEY secret to authenticate the Clore marketplace'
                   if status == 401 and not has_key else
                   'Clore request failed; check CLORE_API_KEY and provider availability')
        super().__init__(API, status, message, {}, None)
