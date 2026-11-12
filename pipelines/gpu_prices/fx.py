"""ECB daily reference rates. Failed refreshes never change the cached date."""
from dataclasses import replace
from datetime import date
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from .http import HttpClient
from .schema import validate_offer

URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def valid_snapshot(value):
    if not isinstance(value,dict) or value.get('source') != URL:
        raise ValueError('invalid FX source')
    date.fromisoformat(value['date'])
    rates = value['rates']
    if (value['date'] > date.today().isoformat() or rates.get('EUR') != 1 or 'USD' not in rates
            or any(isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) or v <= 0 for v in rates.values())):
        raise ValueError('invalid cached FX rates')
    return value


def parse_rates(content):
    root = ET.fromstring(content)
    dated = [node for node in root.iter() if "time" in node.attrib]
    if len(dated) != 1:
        raise ValueError("ECB XML must have one rate date")
    day = date.fromisoformat(dated[0].attrib["time"]).isoformat()
    rates = {node.attrib["currency"]: float(node.attrib["rate"]) for node in dated[0] if "currency" in node.attrib}
    rates["EUR"] = 1.0
    if "USD" not in rates or any(not math.isfinite(v) or v <= 0 for v in rates.values()):
        raise ValueError("invalid ECB rates")
    return {"date": day, "source": URL, "rates": rates}


def refresh(path, *, client=None):
    path = Path(path)
    try:
        previous = valid_snapshot(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else None
    except (OSError,ValueError,KeyError,TypeError):
        previous = None
    try:
        current = valid_snapshot(parse_rates((client or HttpClient()).get_bytes(URL, accept="application/xml")))
        if current["date"] > date.today().isoformat() or (previous and current["date"] < previous["date"]):
            raise ValueError("ECB date moved backwards or into the future")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(current, indent=2), encoding="utf-8")
        temp.replace(path)
        return current, "fresh"
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError):
        if previous:
            return previous, "cached"
        return None, "unavailable"


def to_usd(offer, rates):
    if offer.currency == "USD":
        return offer
    if not rates or rates["date"] > offer.day or offer.currency not in rates["rates"]:
        return offer  # Display native currency; never invent a USD equivalent.
    rate = rates["rates"]["USD"] / rates["rates"][offer.currency]
    return validate_offer(replace(offer, original_currency=offer.currency, original_price=offer.price_per_hour,
        currency="USD", price_per_hour=offer.price_per_hour * rate,
        price_per_gpu_hour=offer.price_per_gpu_hour * rate if offer.price_per_gpu_hour is not None else None,
        fx_usd_per_eur=rates['rates']['USD'], fx_original_per_eur=rates['rates'][offer.currency],
        fx_rate=rate, fx_date=rates["date"], fx_source=rates["source"]))
