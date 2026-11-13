"""Provider adapters for official outbound destinations; no prices or rankings.

Sources checked 2026-11-12: Azure exact series URLs are the Microsoft Learn
sources in catalog.py; fallback https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/
Vast official pricing/model catalog: https://vast.ai/pricing (H100, H200, A100).
Vast referral program: https://github.com/vast-ai/docs/blob/main/guides/reference/referral-program.mdx
Only adapters with a reviewed referral parameter may attach an environment ID.
Azure Learn has no reviewed affiliate contract, so no invented referral query.
UTMs are safe on these ordinary HTTPS pages; no portal/hash-router links used.
"""

import os
import re
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .catalog import AZURE_SKUS
from .providers.registry import PROVIDERS

PROVIDER_LINKS = {
    "aws": {"name": "AWS EC2", "url": "https://aws.amazon.com/ec2/pricing/on-demand/", "ref_param": None},
    "gcp": {"name": "Google Cloud", "url": "https://cloud.google.com/products/compute/gpus-pricing", "ref_param": None},
    "oracle": {"name": "Oracle Cloud", "url": "https://www.oracle.com/cloud/price-list/", "ref_param": None},
    "ovh": {"name": "OVHcloud", "url": "https://www.ovhcloud.com/en/public-cloud/prices/", "ref_param": None},
    "vultr": {"name": "Vultr", "url": "https://www.vultr.com/products/cloud-gpu/", "ref_param": None},
    "clore": {"name": "Clore.ai", "url": "https://clore.ai/marketplace", "ref_param": None},
    "azure": {"name": "Azure", "url": "https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/", "ref_param": None},
    "vast": {"name": "Vast.ai", "url": "https://vast.ai/pricing", "ref_param": "ref_id"},
    "together": {"name": "Together", "url": "https://www.together.ai/pricing", "ref_param": None},
    "linode": {"name": "Linode / Akamai", "url": "https://www.akamai.com/cloud/pricing/gpu", "ref_param": None},
    "scaleway": {"name": "Scaleway", "url": "https://www.scaleway.com/en/pricing/gpu/", "ref_param": None},
    "verda": {"name": "Verda / DataCrunch", "url": "https://verda.com/pricing", "ref_param": None},
}
PROVIDER_ONLY = (
    {"provider": "vast", "name": "Vast.ai", "pricing_url": "https://vast.ai/pricing",
     "gpu_models": ("H100", "H200", "A100"), "note": "provider_live_note"},
    {"provider": "together", "name": "Together", "pricing_url": "https://www.together.ai/pricing",
     "gpu_models": (), "note": "link_only_note"},
)

# Only the exact reviewed page destinations, with no invented referral contract.
for _key, _entry in PROVIDERS.items():
    if _key not in PROVIDER_LINKS and (_entry['tier'] in {'B', 'C'} or _entry.get('link_only')):
        PROVIDER_LINKS[_key] = {'name': _entry['name'], 'url': _entry['source'], 'ref_param': None}
PROVIDER_ONLY += tuple({'provider': key, 'name': entry['name'], 'pricing_url': entry['source'],
                       'gpu_models': (), 'note': entry['note']}
                      for key, entry in PROVIDERS.items() if entry.get('link_only'))


AFFILIATES_CONFIG = Path(__file__).resolve().parents[2] / 'config' / 'affiliates.json'
PENDING_PROVIDERS = frozenset({'novita', 'cloudclusters', 'gpuoncloud', 'massedcompute', 'hyperbolic'})


def link_for(provider, sku, region, price_type, *, model=None):
    """Return {url,label,is_affiliate}; label is a localizable message key.

    region/type are adapter inputs, not fabricated destination parameters.
    Unknown providers fail closed; future adapters add a reviewed table entry.
    Environment is read at call/build time, without accessing .env or vault/.
    """
    entry = PROVIDER_LINKS.get(provider)
    if entry is None:
        raise ValueError("No reviewed outbound link for provider: " + provider)
    spec = AZURE_SKUS.get(sku) if provider == "azure" else None
    url = spec.source_url if spec else entry["url"]
    campaign = model or (spec.gpu_model if spec else provider)
    config = json.loads(AFFILIATES_CONFIG.read_text(encoding='utf-8')) if AFFILIATES_CONFIG.exists() else {}
    setting = config.get(provider, {})
    if provider in PENDING_PROVIDERS:
        # Activation requires BOTH a real ID and an explicitly configured URL.
        # Pending links stay byte-for-byte plain, without UTMs or guessed IDs.
        affiliate_url = setting.get('affiliate_url', '')
        affiliate = setting.get('status') == 'active' and bool(str(setting.get('ref_id', '')).strip()) and bool(affiliate_url)
        if affiliate:
            target = urlsplit(affiliate_url)
            if (target.scheme != 'https' or not target.hostname or target.username or target.password
                    or any(ord(c) < 33 for c in affiliate_url)):
                raise ValueError('Invalid configured affiliate URL for ' + provider)
            url = affiliate_url
        return {'url': url, 'label': 'view_provider', 'provider_name': entry['name'], 'is_affiliate': bool(affiliate)}
    ref = os.environ.get("AFFILIATE_" + re.sub(r"[^A-Z0-9]", "_", provider.upper()) + "_REF", "").strip()
    if provider == "vast" and "AFFILIATE_VAST_REF" not in os.environ:
        ref = str(config.get('vast', {}).get('ref_id', '')).strip()
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    affiliate = bool(ref and entry["ref_param"])
    if affiliate:
        # Referrals belong to the console, not the marketing pricing page.
        if provider == "vast":
            parts = urlsplit("https://cloud.vast.ai/")
        params[entry["ref_param"]] = ref
    if provider not in {'clore', 'anygpu', 'ai_galaxy', 'sesterce', 'seeweb', 'gcore', 'jarvislabs', 'modal', 'koyeb'}:
        params.update(utm_source="gpu-cloud-prices", utm_medium="referral", utm_campaign=campaign)
    return {"url": urlunsplit(parts._replace(query=urlencode(params))),
            "label": "view_provider", "provider_name": entry["name"], "is_affiliate": affiliate}
