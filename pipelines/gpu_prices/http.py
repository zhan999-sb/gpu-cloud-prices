"""Allowlisted HTTP boundary; optional Clore auth stays on api.clore.ai."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
import hashlib
import json
import logging
import random
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from .clore_auth import auth_headers, CloreRequestError

LOG = logging.getLogger(__name__)
USER_AGENT = "gpu-price-index/0.1 (+contact in site footer)"
PAGE_ENDPOINTS = {
    "www.anygpu.cn": {"/"},
    "www.ai-galaxy.cn": {"/"},
    "api.clore.ai": {"/v1/marketplace"},
    "featurize.cn": {"/vm/available"},
    "nebius.com": {"/prices"},
    "lambda.com": {"/pricing"},
    "www.paperspace.com": {"/pricing"},
    "cloud.baidu.com": {"/product-price/gpu.html"},
    "vm.massedcompute.com": {"/pricing"},
    "www.cloudclusters.io": {"/server/gpu"},
}
ENDPOINTS = {
    **PAGE_ENDPOINTS,
    "prices.azure.com": {"/api/retail/prices"},
    "api.linode.com": {"/v4/linode/types", "/v4/regions", "/v4/regions/availability"},
    "api.scaleway.com": {"/product-catalog/v2alpha1/public-catalog/products"},
    "api.verda.com": {"/v1/instance-types"},
    "api.vultr.com": {"/v2/plans"},
    "apexapps.oracle.com": {"/pls/apex/cetools/api/v1/products/"},
    "eu.api.ovh.com": {"/1.0/order/catalog/public/cloud"},
    "www.ecb.europa.eu": {"/stats/eurofxref/eurofxref-daily.xml"},
}
ENDPOINTS['api.vultr.com'].add('/v2/plans-metal')
ENDPOINTS['pricing.us-east-1.amazonaws.com'] = {'/offers/v1.0/aws/AmazonEC2/current/region_index.json'}
ENDPOINTS['website.spot.ec2.aws.a2z.com'] = {'/spot.json'}
ENDPOINTS['www.hetzner.com'] = {'/dedicated-rootserver/matrix-gpu/'}
BOUNDED_HOSTS = frozenset({'pricing.us-east-1.amazonaws.com', 'website.spot.ec2.aws.a2z.com',
                         'api.vultr.com', 'apexapps.oracle.com', 'eu.api.ovh.com', 'www.hetzner.com'})
ALLOWED_HOSTS = frozenset(ENDPOINTS)
TIMEOUT = 60
ATTEMPTS = 5


def validate_url(url: str) -> str:
    """Fail closed on initial URLs AND NextPageLink; allow documented :443 links."""
    if not isinstance(url, str) or any(ord(c) < 33 for c in url):
        raise ValueError("Invalid API URL")
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == "https" and parts.hostname in ALLOWED_HOSTS
                 and parts.port in (None, 443) and not parts.username
                 and not parts.password and not parts.fragment
                 and (parts.path in ENDPOINTS.get(parts.hostname, set()) or
                      parts.hostname == 'pricing.us-east-1.amazonaws.com' and
                      re.fullmatch(r'/offers/v1\.0/aws/AmazonEC2/(?:current|[0-9]{14})/[a-z]{2}-[a-z]+-[1-9]/index\.csv', parts.path)))
    except ValueError as exc:
        raise ValueError("Invalid API URL") from exc
    if not valid:
        raise ValueError("API URL is not on the approved endpoint allowlist")
    return url


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Even a seemingly harmless redirect must never trigger an unvetted request.
        raise ValueError("HTTP redirects are refused; use the approved API endpoint")


class HttpClient:
    def __init__(self, *, opener=None, sleep=time.sleep, jitter=random.random, now=None):
        self.bytes_read = 0
        self.stream_metrics = []
        self._opener = opener or build_opener(_NoRedirects()).open
        self._sleep = sleep
        self._jitter = jitter
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _retry_after(self, value: str | None) -> float:
        if not value:
            return 0.0
        try:
            return max(0.0, float(int(value)))
        except (ValueError, OverflowError):
            try:
                target = parsedate_to_datetime(value)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                return max(0.0, (target - self._now()).total_seconds())
            except (ValueError, TypeError, OverflowError):
                return 0.0

    def get_bytes(self, url: str, *, accept="application/json") -> bytes:
        validate_url(url)  # Validation errors never retry or send a request.
        parts = urlsplit(url)
        is_clore = parts.hostname == 'api.clore.ai'
        credentials = auth_headers(url) if is_clore else {}
        # Page requests have a DB-backed hourly budget. A transient failure also
        # consumes it: do not issue extra requests inside the retry loop.
        attempts = 1 if parts.hostname in BOUNDED_HOSTS or parts.path in PAGE_ENDPOINTS.get(parts.hostname, set()) else ATTEMPTS
        for attempt in range(attempts):
            retry_after = 0.0
            try:
                request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept, **credentials})
                with self._opener(request, timeout=30 if parts.hostname in BOUNDED_HOSTS else TIMEOUT) as response:
                    validate_url(response.geturl())
                    if (urlsplit(response.geturl()).hostname, urlsplit(response.geturl()).path) != (urlsplit(url).hostname, urlsplit(url).path):
                        raise ValueError("Response moved to a different endpoint")
                    if parts.hostname in BOUNDED_HOSTS:
                        result = response.read(16_000_001)
                        if len(result) > 16_000_000:
                            raise ValueError('catalog exceeds 16 MB budget')
                        self.bytes_read += len(result)
                        return result
                    return response.read()
            except HTTPError as exc:
                if is_clore:
                    status = exc.code
                    exc.close()
                    raise CloreRequestError(status, bool(credentials)) from None
                retry_after = self._retry_after(exc.headers.get("Retry-After")) if exc.headers else 0.0
                status = exc.code
                if status not in (408, 429, 500, 502, 503, 504):
                    LOG.error("HTTP %d response: %s", status, exc.read(4096).decode("utf-8", errors="replace"))
                exc.close()
                if status not in (408, 429, 500, 502, 503, 504):
                    raise
                error = exc
            except (URLError, TimeoutError, ConnectionError, OSError, IncompleteRead) as exc:
                if is_clore:
                    raise CloreRequestError(0, bool(credentials)) from None
                error = exc
            except ValueError:
                if is_clore:
                    raise CloreRequestError(0, bool(credentials)) from None
                raise
            except Exception:
                if is_clore:
                    raise CloreRequestError(0, bool(credentials)) from None
                raise
            if attempt == attempts - 1:
                raise error
            delay = max(retry_after, 2 ** attempt + self._jitter())
            LOG.warning("HTTP attempt %d/%d failed (%s); retry in %.2fs", attempt + 1, ATTEMPTS, error, delay)
            self._sleep(delay)
        raise AssertionError("unreachable")

    def iter_lines(self, url, *, max_bytes=350_000_000, max_seconds=150):
        """One bounded streaming bulk request. No retries, disk cache or credentials."""
        validate_url(url)
        if urlsplit(url).hostname != 'pricing.us-east-1.amazonaws.com' or not urlsplit(url).path.endswith('/index.csv'):
            raise ValueError('streaming requires an approved EC2 regional CSV')
        started, size = time.monotonic(), 0
        observed_at = self._now().isoformat()
        digest = hashlib.sha256()
        with self._opener(Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'text/csv'}), timeout=30) as response:
            if response.geturl() != url:
                raise ValueError('bulk response moved')
            length = response.headers.get('Content-Length')
            if length and int(length) > max_bytes:
                raise ValueError('bulk catalog exceeds byte budget')
            while True:
                line = response.readline(65537)
                if not line:
                    break
                size += len(line)
                self.bytes_read += len(line)
                digest.update(line)
                if len(line) > 65536 or size > max_bytes or time.monotonic() - started > max_seconds:
                    raise ValueError('bulk catalog exceeds byte, line or time budget')
                yield line.decode('utf-8')
        self.stream_metrics.append({'url': url, 'bytes': size, 'observed_at': observed_at,
                                    'seconds': round(time.monotonic()-started, 3), 'sha256': digest.hexdigest()})

    def get_json(self, url: str) -> dict | list:
        data = self.get_bytes(url)
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeError):
            if urlsplit(url).hostname == 'api.clore.ai':
                raise CloreRequestError(0, bool(auth_headers(url))) from None
            raise
        if not isinstance(payload, (dict, list)):
            raise ValueError("API response must be a JSON object or array")
        return payload
