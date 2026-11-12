"""Provider-independent hourly offers; RAM follows Azure's GiB convention."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import math
import re
from urllib.parse import urlsplit

PRICE_TYPES = frozenset({"on_demand", "spot", "interruptible", "reserved", "reserved_1y", "reserved_3y"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("collected_at must be a UTC ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid UTC ISO timestamp") from exc
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must have an explicit UTC offset")
    return parsed


@dataclass(frozen=True, slots=True)
class Offer:
    provider: str
    sku: str
    gpu_model: str
    gpu_count: float | None  # Explicit allocation; unknown is never inferred from billing.
    vcpu: int | None
    ram_gb: float | None  # Historical field name; values are GiB.
    region: str | None
    price_type: str
    currency: str
    price_per_hour: float
    price_per_gpu_hour: float | None
    source_url: str
    collected_at: str
    effective_start_date: str | None = None  # Unknown in older snapshots; never fabricate.
    price_scope: str = "instance"
    gpu_fraction: float | None = 1.0
    gpu_model_raw: str | None = None
    tax_basis: str = "unknown"
    is_from_price: bool = False
    billing_unit: str = "instance-hour"
    minimum_billing_seconds: int | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    availability: str = "unknown"
    availability_observed_at: str | None = None
    tier: str = "A"
    export_allowed: bool = True
    original_currency: str | None = None
    original_price: float | None = None
    fx_rate: float | None = None
    fx_date: str | None = None
    fx_source: str | None = None
    product: str | None = None
    zone: str | None = None
    cpu_raw: str | None = None
    ram_raw: str | None = None
    vram_raw: str | None = None
    price_conditions: str | None = None
    price_per_day: float | None = None  # Original whole-server Clore quote.
    source_billing_unit: str | None = None
    price_basis: str | None = None
    fx_usd_per_eur: float | None = None
    fx_original_per_eur: float | None = None

    def current_at(self, stamp: str) -> bool:
        now = parse_utc(stamp)
        start = self.effective_from or self.effective_start_date
        return ((not start or parse_utc(start) <= now)
                and (not self.effective_to or now < parse_utc(self.effective_to)))

    @property
    def day(self) -> str:
        return parse_utc(self.collected_at).date().isoformat()

    def to_dict(self) -> dict:
        return asdict(self)


def validate_offer(offer: Offer) -> Offer:
    """Return the offer, or raise ValueError. Zero/NaN placeholder rates are invalid."""
    for name in ("provider", "sku", "gpu_model", "region"):
        value = getattr(offer, name)
        if name == "region" and value is None:
            continue
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"{name} must be a nonempty, trimmed string")
    if not isinstance(offer.price_type, str) or offer.price_type not in PRICE_TYPES:
        raise ValueError("unsupported price_type")
    if not isinstance(offer.currency, str) or not re.fullmatch(r"[A-Z]{3}", offer.currency):
        raise ValueError("currency must be an uppercase three-letter code")
    if offer.vcpu is not None and (type(offer.vcpu) is not int or offer.vcpu <= 0):
        raise ValueError("vcpu must be a positive integer")
    for name in ("gpu_count", "ram_gb", "price_per_hour", "price_per_gpu_hour", "gpu_fraction"):
        value = getattr(offer, name)
        if name in {"gpu_count", "ram_gb", "price_per_gpu_hour", "gpu_fraction"} and value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if offer.price_scope not in {"instance", "per_gpu", "gpu_component"}:
        raise ValueError("invalid price_scope")
    if (offer.gpu_fraction is not None and offer.gpu_fraction > 1) or offer.tax_basis not in {"excluded", "included", "unknown"}:
        raise ValueError("invalid GPU fraction or tax basis")
    if offer.tier not in {"A", "B", "C"} or (offer.export_allowed and offer.tier != "A"):
        raise ValueError("only tier A is exportable; tier D cannot be ingested")
    if type(offer.is_from_price) is not bool or type(offer.export_allowed) is not bool:
        raise ValueError("policy flags must be booleans")
    if offer.billing_unit not in {"instance-hour", "GPU-hour", "gpu-hour", "instance-minute", "instance-second", "GPU-second"}:
        raise ValueError("non-hourly prices require explicit normalization; monthly prices are refused")
    if offer.minimum_billing_seconds is not None and (type(offer.minimum_billing_seconds) is not int or offer.minimum_billing_seconds <= 0):
        raise ValueError("invalid minimum_billing_seconds")
    allocation = offer.gpu_count or (offer.gpu_fraction if offer.gpu_fraction is not None and offer.gpu_fraction < 1 else None)
    expected = (offer.price_per_hour / allocation if allocation else None) if offer.price_scope == "instance" else offer.price_per_hour
    if ((expected is None) != (offer.price_per_gpu_hour is None)
            or expected is not None and not math.isclose(offer.price_per_gpu_hour, expected, rel_tol=1e-9, abs_tol=1e-12)):
        raise ValueError("price_per_gpu_hour must match the explicit allocation and price scope")
    if not isinstance(offer.source_url, str) or any(ord(c) < 33 for c in offer.source_url):
        raise ValueError("source_url must be a public HTTPS URL")
    source = urlsplit(offer.source_url)
    if source.scheme != "https" or not source.hostname or source.username or source.password or source.port not in (None, 443):
        raise ValueError("source_url must be a public HTTPS URL without credentials")
    parse_utc(offer.collected_at)
    if offer.provider == 'clore':
        if (offer.source_billing_unit != 'instance-day' or not offer.price_basis
                or offer.price_per_day is None or not math.isfinite(offer.price_per_day)
                or offer.price_per_day <= 0 or offer.currency != 'USD'
                or not math.isclose(offer.price_per_hour, offer.price_per_day / 24, rel_tol=1e-9)):
            raise ValueError('invalid Clore daily price provenance')
    if offer.effective_start_date is not None:
        parse_utc(offer.effective_start_date)
    for value in (offer.effective_from, offer.effective_to, offer.availability_observed_at):
        if value is not None:
            parse_utc(value)
    if offer.effective_from and offer.effective_to and parse_utc(offer.effective_from) >= parse_utc(offer.effective_to):
        raise ValueError("empty effective interval")
    if offer.original_currency is not None:
        if not re.fullmatch(r"[A-Z]{3}", offer.original_currency) or offer.original_price is None or not math.isfinite(offer.original_price) or offer.original_price <= 0:
            raise ValueError("invalid original price")
        if offer.original_currency != offer.currency:
            from datetime import date
            if not offer.fx_rate or not math.isfinite(offer.fx_rate) or offer.fx_rate <= 0 or offer.fx_source != 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml' or not offer.fx_date:
                raise ValueError("converted prices require FX provenance")
            date.fromisoformat(offer.fx_date)
            if offer.fx_date > offer.day or not math.isclose(offer.price_per_hour, offer.original_price * offer.fx_rate, rel_tol=1e-9):
                raise ValueError("invalid FX date or conversion")
    return offer
