"""Independent build gate. Reads SQLite, never renderer/store/statistics helpers.

Only the reviewed region DATA table and translation catalogs are shared. Prices
are divided again from VM dollars and GPU count; a hand-written median is used.
HTML values carry semantic pointers so every table, card and SVG value can be
checked against recomputed facts. SEO/FAQ prose and graph geometry get separate
checks. No expected-value manifest or rendered JSON is used as a price oracle.
"""

from datetime import date, datetime, timezone
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import random
import re
import sqlite3
from urllib.parse import urlencode

from .i18n import LANGUAGES, load_catalogs
from .regions import REGIONS
from .catalog import AZURE_SKUS
from .links import PROVIDER_ONLY
from .providers.registry import PROVIDERS
from fractions import Fraction

SOURCE = "https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices"
FIELDS = "provider sku gpu_model gpu_count vcpu ram_gb region price_type currency price_per_hour price_per_gpu_hour source_url collected_at effective_start_date price_scope gpu_fraction gpu_model_raw tax_basis is_from_price billing_unit minimum_billing_seconds effective_from effective_to availability availability_observed_at tier export_allowed original_currency original_price fx_rate fx_date fx_source product zone cpu_raw ram_raw vram_raw".split()


class AuditError(ValueError):
    pass


def _rows(connection, sql):
    cursor = connection.execute(sql)
    names = [col[0] for col in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _classification(name):
    if name is None:
        return "unknown_region"
    return "edge" if name.lower().startswith("att") else REGIONS.get(name.lower(), "standard")


def _eligible(row):
    return (row["currency"] == "USD" and row["gpu_count"] >= 1 and float(row['gpu_count']).is_integer()
            and row.get('gpu_fraction',1) == 1 and not row.get('is_from_price',False)
            and row.get('tax_basis','unknown') != 'included' and row.get('price_scope','instance') in {'instance','per_gpu'}
            and _classification(row["region"]) == "standard" and row["price_type"] in ("on_demand", "spot"))


def _rate(row):
    return row['price_per_hour'] / row['gpu_count'] if row.get('price_scope','instance') == 'instance' else row['price_per_hour']


def _current(row, stamp):
    parse = lambda v: datetime.fromisoformat(v.replace('Z','+00:00'))
    start = row.get('effective_from') or row.get('effective_start_date')
    return (not start or parse(start) <= parse(stamp)) and (not row.get('effective_to') or parse(stamp) < parse(row['effective_to']))


def _distribution(rows):
    values = sorted(_rate(r) for r in rows)
    if not values:
        return None
    n = len(values)
    middle = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2
    return {"min": values[0], "median": middle, "max": values[-1], "count": n}


def _statistics(rows):
    good = [r for r in rows if _eligible(r)]
    distributions = {kind: _distribution([r for r in good if r["price_type"] == kind]) for kind in ("on_demand", "spot")}
    cheapest = {kind: values["min"] if values else None for kind, values in distributions.items()}
    regions = []
    for name in sorted({r["region"] for r in rows}, key=lambda r: r or ""):
        for allocation in ("whole", "fractional"):
            local = [r for r in rows if r["region"] == name and (r["gpu_count"] >= 1 and float(r["gpu_count"]).is_integer() and r["gpu_fraction"] == 1) == (allocation == "whole")]
            if local:
                entry = {"region": name, "classification": _classification(name), "allocation": allocation, "offers": len(local)}
                for kind in ("on_demand", "spot"):
                    values = [_rate(r) for r in local if r["price_type"] == kind and r["currency"] == "USD" and r.get("tax_basis") != "included" and not r.get("is_from_price") and r.get("price_scope") != "gpu_component"]
                    entry[kind + "_min"] = min(values) if values else None
                regions.append(entry)
    demand, spot = cheapest["on_demand"], cheapest["spot"]
    both = demand is not None and spot is not None
    return {"offers": len(good), "all_offers": len(rows), "region_count": len({r["region"] for r in good}),
            "all_region_count": len({r["region"] for r in rows}), "cheapest": cheapest, "distributions": distributions,
            "spot_discount_percent": 100 * (1 - spot / demand) if both and spot < demand else None,
            "spot_no_discount": both and spot >= demand, "regions": regions}


def _snapshot(connection, day, generated):
    rows = _rows(connection, "SELECT * FROM offers")
    for row in rows:
        row['region'] = row['region'] or None
        for key in ('is_from_price','export_allowed'):
            row[key] = bool(row[key])
    runs = _rows(connection, "SELECT * FROM collection_runs ORDER BY id")
    run = runs[-1] if runs else None
    providers = {r['provider']:r for r in runs}
    stamp = lambda value: datetime.fromisoformat(value.replace('Z','+00:00'))
    current = []
    for row in rows:
        if row['day'] != day or not _current(row, generated):
            continue
        last = providers.get(row['provider'])
        if last and (last['status']=='failed' or last['started_at'][:10] != day):
            continue
        if last and last['status']=='complete' and not stamp(last['started_at']) <= stamp(row['collected_at']) <= stamp(last['finished_at']):
            continue
        current.append(row)
    current.sort(key=lambda r: (r['currency'],r['price_per_gpu_hour'],r['provider'],r['region'] or '',r['sku'],r['price_type']))
    models, exports = {}, {}
    def exported(row):
        entry = {key:row.get(key) for key in FIELDS}
        entry['price_per_gpu_hour'] = _rate(row)
        return entry
    def history(name, active, export_only):
        result = {}
        for kind in ('on_demand','spot'):
            grouped = {}
            for row in rows:
                if row['gpu_model'] == name and row['day'] != day and row['price_type']==kind and _eligible(row) and _current(row,row['day']+'T23:59:59.999999Z') and (not export_only or row['tier']=='A' and row['export_allowed']):
                    grouped.setdefault(row['day'],[]).append(row)
            good = [r for r in active if _eligible(r) and r['price_type']==kind]
            if good:
                grouped[day] = good
            result[kind] = [{'day':d,'currency':'USD','price_type':kind,**_distribution(grouped[d])} for d in sorted(grouped)]
        return result
    for name in sorted({r['gpu_model'] for r in rows}):
        offers = [r for r in current if r['gpu_model']==name]
        updated = max((r['collected_at'] for r in offers),default=max(r['collected_at'] for r in rows if r['gpu_model']==name))
        data = {'gpu_model':name,'currency':next(iter({r['currency'] for r in offers})) if len({r['currency'] for r in offers})==1 else 'USD' if not offers else 'MULTI','observation_day':day,'last_updated':updated,
                'generated_at':generated,'source':sorted({PROVIDERS[r['provider']]['source'] for r in offers if r['provider'] in PROVIDERS}) or [SOURCE],'latest_collection':run,
                'offers':[exported(r) for r in offers],'statistics':_statistics(offers),'history':history(name,offers,False)}
        models[name] = data
        permitted = [r for r in offers if r['tier']=='A' and r['export_allowed']]
        history_rows = [r for r in rows if r['gpu_model']==name and r['tier']=='A' and r['export_allowed']]
        if history_rows:
            exports[name] = {**data,'last_updated':max(r['collected_at'] for r in history_rows),
                'currency':next(iter({r['currency'] for r in permitted})) if len({r['currency'] for r in permitted})==1 else 'USD' if not permitted else 'MULTI',
                'latest_collection':max((r for r in providers.values() if r['provider'] in {o['provider'] for o in history_rows}),key=lambda r:r['id'],default=None),
                'source':sorted({PROVIDERS[r['provider']]['source'] for r in history_rows if r['provider'] in PROVIDERS}) or [SOURCE],
                'offers':[exported(r) for r in permitted],'statistics':_statistics(permitted),'history':history(name,permitted,True)}
    good = [r for r in current if _eligible(r)]
    coverage = []
    for key, provider in PROVIDERS.items():
        last = providers.get(key)
        status = ('link_only' if provider['tier']=='D' else 'disabled') if not provider['enabled'] else ('stale_provider' if last and (last['status']=='failed' or last['started_at'][:10]!=day) else last['status'] if last else 'not_collected')
        coverage.append({'provider':key,'name':provider['name'],'tier':provider['tier'],'status':status,
            'model_count':len({r['gpu_model'] for r in rows if r['provider']==key}),
            'last_success':max((r['finished_at'] for r in runs if r['provider']==key and r['status']=='complete'),default=None)})
    return {'models':models,'exports':exports,'coverage':coverage,'date':day,'built':generated,'run':run,
            'summary':{'models':len({r['gpu_model'] for r in good}),'offers':len(good),'regions':len({r['region'] for r in good})},
            'all_rows':rows,'last_updated':max((r['collected_at'] for r in rows),default=None),
            'current_updated':max((r['collected_at'] for r in current),default=None)}


def _compare(actual, expected, path, errors):
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or actual.keys() != expected.keys():
            errors.append(path + ": object keys differ")
            return
        for key, value in expected.items():
            _compare(actual[key], value, path + "/" + str(key), errors)
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            errors.append(path + ": list length differs")
            return
        for i, value in enumerate(expected):
            _compare(actual[i], value, path + "/" + str(i), errors)
    elif isinstance(expected, (float, int)) and not isinstance(expected, bool):
        if type(actual) not in (float, int) or not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12):
            errors.append(f"{path}: expected {expected!r}, found {actual!r}")
    elif actual != expected:
        errors.append(f"{path}: expected {expected!r}, found {actual!r}")


def _resolve(facts, pointer):
    value = facts
    for part in pointer.lstrip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def _dollars(value):
    return "$" + format(value, ".2f" if value >= 1 else ".3f")


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.stack, self.values, self.metadata, self.structured, self.times, self.graphs = [], [], {}, [], [], []
        self.faqs, self.title, self.text, self.unmarked = {}, "", "", []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            self.metadata[a.get("name", a.get("property"))] = a.get("content")
        if "data-bar" in a or "data-series" in a:
            self.graphs.append(a)
        if tag not in {"meta", "link", "br", "hr", "input", "img"}:
            self.stack.append([tag, a, []])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in {"meta", "link", "br", "hr", "input", "img"}:
            self.handle_endtag(tag)

    def handle_data(self, data):
        for frame in self.stack:
            frame[2].append(data)
        if not any(f[0] in ("script", "style", "head") for f in self.stack):
            self.text += data
            if re.search(r"[0-9$%]", data) and not any("data-stat" in f[1] or "data-faq" in f[1] or f[0] == "time" for f in self.stack):
                self.unmarked.append(data.strip())

    def handle_endtag(self, tag):
        if not self.stack:
            return
        kind, a, chunks = self.stack.pop()
        text = ''.join(chunks)
        if "data-stat" in a:
            self.values.append((a["data-stat"], a.get("data-format"), text))
        if "data-faq" in a:
            self.faqs[int(a["data-faq"])] = text
        if kind == "title" and not any(f[0] == "svg" for f in self.stack):
            self.title = text
        if kind == "time":
            self.times.append((a["datetime"], text))
        if kind == "script" and a.get("type") == "application/ld+json":
            self.structured.append(json.loads(text))


def _metadata(tr, model, data, facts):
    day = facts["date"]
    summary = facts["summary"]
    numbers = dict(count=tr.quantity("offer_count", summary["offers"]), models=tr.quantity("model_count", summary["models"]), regions=tr.quantity("region_count", summary["regions"]), date=day)
    if model is None:
        return tr.text("home_title", **numbers), tr.text("home_description", **numbers)
    if model == "/methodology":
        # Method metadata describes the comparison basis, like the index.
        return tr.text("method_title"), tr.text("method_description", **numbers)
    if model == "/404.html":
        return tr.text("not_found"), tr.text("not_found_description")
    stats = data["statistics"]
    if data["offers"]:
        title = tr.text("model_title", model=model,
                        price=_dollars(stats["cheapest"]["on_demand"]) if stats["cheapest"]["on_demand"] is not None else tr.text("not_observed"),
                        spot_price=_dollars(stats["cheapest"]["spot"]) if stats["cheapest"]["spot"] is not None else tr.text("not_observed"),
                        count=tr.quantity("offer_count", stats["offers"]), regions=tr.quantity("azure_region_count", stats["region_count"]), date=data["last_updated"][:10])
    else:
        title = tr.text("model_empty_title", model=model, count=tr.quantity("offer_count", 0), regions=tr.quantity("azure_region_count", 0), date=day)
        if data["last_updated"]:
            title = tr.text("last_observed", title=title, date=data["last_updated"][:10])
    return title, tr.text("model_description", title=title)


def _faq_expected(tr, name, data, day):
    stats = data["statistics"]
    demand, spot = (stats["cheapest"][k] for k in ("on_demand", "spot"))
    percent = stats["spot_discount_percent"]
    comparison = tr.text("no_discount") if stats["spot_no_discount"] else tr.text("discount_below", percent=format(percent, ".2f")) if percent is not None else tr.text("not_observed")
    questions = [tr.text("faq_cheapest_q", model=name), tr.text("faq_spot_q"), tr.text("faq_units_q")]
    answers = [tr.text("faq_cheapest_a", demand=_dollars(demand) if demand is not None else tr.text("not_observed"), spot=_dollars(spot) if spot is not None else tr.text("not_observed"), regions=stats["region_count"], offers=stats["offers"], date=day), tr.text("faq_spot_a", comparison=comparison), tr.text("faq_units_a")]
    return questions, answers


def _allowed_literals(tr, facts):
    """Numeric text outside value spans must be a known source identifier or
    an exact localized definition. An extra unmarked '$9.99' fails the audit.
    """
    allowed = {v for v in tr.messages.values() if '{' not in v}
    allowed.add("zhan999 [at] tutamail [dot] com")
    allowed.add(tr.text("basis") + " " + tr.text("maximum_def"))
    allowed.add(tr.text("model_intro", date=facts["date"]))
    for kind in ("on_demand", "spot"):
        allowed.add(tr.text("chart_title", kind=tr.text(kind)))
        allowed.add(tr.text("history_caption", kind=tr.text(kind)))
    for model in facts["models"]:
        allowed.update((model, tr.text("faq_heading", model=model), tr.text("faq_cheapest_q", model=model), model + " / " + tr.text("on_demand_from")))
        stats = facts["models"][model]["statistics"]
        count = tr.text("offer_count_one" if stats["offers"] == 1 else "offer_count_other", count="|VALUE|")
        regions = tr.text("region_count_one" if stats["region_count"] == 1 else "region_count_other", count="|VALUE|")
        text = tr.text("observed_summary", model=model, date="|VALUE|", count=count, regions=regions)
        allowed.update(part.strip() for part in text.split("|VALUE|"))
    for row in facts["all_rows"]:
        allowed.update((row["sku"], row["region"]))
        allowed.update(tr.text(k) + ": " + (row["region"] or tr.text("unknown_region")) for k in ("on_demand", "spot"))
        if row["gpu_count"] < 1 or row["gpu_fraction"] < 1:
            allowed.add(tr.text("fraction_note", share=str(Fraction(min(row["gpu_count"],row["gpu_fraction"])).limit_denominator(12))))
    allowed.update(spec.source_url.rsplit('/', 1)[-1] for spec in AZURE_SKUS.values())
    allowed.update(' / '.join(provider["gpu_models"]) for provider in PROVIDER_ONLY)
    # Home always presents these KPI labels even if not yet tracked.
    allowed.update(model + " / " + tr.text("on_demand_from") for model in ("H100 SXM 80GB", "H200 141GB", "A100 80GB"))
    return allowed


def audit_site(connection_or_path, out):
    """Raise AuditError on any mismatch; return a compact, reproducible report."""
    own = not isinstance(connection_or_path, sqlite3.Connection)
    conn = sqlite3.connect(Path(connection_or_path).resolve().as_uri() + "?mode=ro", uri=True) if own else connection_or_path
    try:
        return _audit(conn, Path(out))
    finally:
        if own:
            conn.close()


def _audit(conn, out):
    manifest = json.loads((out / "gpu-prices-manifest.json").read_text(encoding="utf-8"))
    facts = _snapshot(conn, manifest["observation_day"], manifest["generated_at"])
    errors, checked, html_pages = [], 0, 0
    # Independently verify the new semantic/provenance contract in SQLite.
    for row in facts['all_rows']:
        key = row['provider'] + '/' + row['sku']
        if row['tier'] not in {'A','B','C'} or row['export_allowed'] and row['tier'] != 'A':
            errors.append(key + ': tier/export policy violation')
        if row['billing_unit'] not in {'instance-hour','instance-minute','instance-second','GPU-hour','gpu-hour','GPU-second'}:
            errors.append(key + ': unresolved billing unit (monthly prices forbidden)')
        if row['price_scope'] not in {'instance','per_gpu','gpu_component'} or not math.isclose(row['price_per_gpu_hour'],_rate(row),rel_tol=1e-9):
            errors.append(key + ': price scope or per-GPU normalization mismatch')
        if row['original_currency'] and row['original_currency'] != row['currency']:
            if (not row['fx_date'] or row['fx_date'] > row['day'] or row['fx_source'] != 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml'
                    or not row['fx_rate'] or not math.isclose(row['original_price']*row['fx_rate'],row['price_per_hour'],rel_tol=1e-9)):
                errors.append(key + ': invalid FX provenance')
    catalogs = load_catalogs()
    expected_files = {"favicon.svg", "og.svg", "sitemap.xml", "robots.txt"}
    slugs = manifest["model_slugs"]
    if slugs.keys() != facts["models"].keys():
        errors.append("manifest: model coverage differs from SQLite")
    for name, data in facts["exports"].items():
        filename = "data/" + slugs[name] + ".json"
        expected_files.add(filename)
        _compare(json.loads((out / filename).read_text(encoding="utf-8")), facts["exports"][name], filename, errors)
        checked += 1
    for lang in LANGUAGES:
        tr = catalogs[lang.code]
        routes = [(None, "index.html"), ("/methodology", "methodology.html"), ("/404.html", "404.html")]
        routes += [(name, slugs[name] + "/index.html") for name in facts["models"]]
        for name, relative in routes:
            filename = (lang.prefix + "/" if lang.prefix else "") + relative
            expected_files.add(filename)
            raw = (out / filename).read_text(encoding="utf-8")
            page = Page(raw)
            data = facts["models"].get(name)
            page_facts = {**facts, "page_updated": data["last_updated"] if data else facts["last_updated"]}
            title, description = _metadata(tr, name, data, facts)
            expected_meta = {k: description for k in ("description", "og:description", "twitter:description")}
            expected_meta.update({k: title for k in ("og:title", "twitter:title")})
            if page.title != tr.text("page_title", title=title, site_name=tr.text("site_name")):
                errors.append(filename + ": title differs from independent statistics")
            for key, value in expected_meta.items():
                if page.metadata.get(key) != value:
                    errors.append(filename + ": " + key + " differs from independent statistics")
            for pointer, fmt, actual in page.values:
                try:
                    value = _resolve(page_facts, pointer)
                    expected = _dollars(value) if fmt == "money" else format(value, ".2f") + "%" if fmt == "percent" else format(value, "g") if fmt == "number" else value[:10] if fmt == "date" else str(value)
                    if actual != expected:
                        errors.append(f"{filename} {pointer}: expected {expected!r}, found {actual!r}")
                except (KeyError, IndexError, TypeError, ValueError):
                    errors.append(filename + ": invalid statistic pointer " + pointer)
                checked += 1
            allowed = _allowed_literals(tr, facts)
            for text in set(page.unmarked):
                if text not in allowed:
                    errors.append(filename + ": unaccounted numeric text " + repr(text))
            present = {p for p, _, _ in page.values}
            if data:
                for i, row in enumerate(data["offers"]):
                    prefix = "/models/" + name.replace("~", "~0").replace("/", "~1") + "/offers/" + str(i) + "/"
                    for field in ("price_per_hour", "price_per_gpu_hour", "gpu_count", "vcpu", "ram_gb"):
                        if row[field] is None:
                            continue
                        if prefix + field not in present:
                            errors.append(filename + ": missing offer value " + prefix + field)
                if data["offers"]:
                    questions, answers = _faq_expected(tr, name, data, facts["date"])
                    if page.faqs != dict(enumerate(answers)):
                        errors.append(filename + ": visible FAQ differs from independent statistics")
                    faq = next((d for d in page.structured if d.get("@type") == "FAQPage"), {})
                    entities = [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in zip(questions, answers)]
                    _compare(faq.get("mainEntity"), entities, filename + "/FAQPage", errors)
                if page.structured[0].get("description") != description:
                    errors.append(filename + ": Dataset description mismatch")
                dataset = {"@context": "https://schema.org", "@type": "Dataset",
                           "@id": manifest["site_base_url"] + lang.path('/' + slugs[name] + '/') + "#dataset",
                           "name": tr.text("dataset_name", model=name), "description": description,
                           "url": manifest["site_base_url"] + lang.path('/' + slugs[name] + '/'),
                           "isBasedOn": data["source"], "inLanguage": lang.code,
                           "spatialCoverage": [{"@type": "Place", "name": r} for r in sorted({r["region"] for r in data["offers"]}, key=lambda r: r or "")],
                           "variableMeasured": {"@type": "PropertyValue", "name": "price_per_gpu_hour", "unitText": "USD/GPU-hour"},
                           }
                if name in facts['exports']:
                    dataset['distribution']={'@type':'DataDownload','encodingFormat':'application/json','contentUrl':manifest['site_base_url']+'/data/'+slugs[name]+'.json'}
                if data["last_updated"]:
                    dataset["dateModified"] = data["last_updated"]
                if data["offers"]:
                    dataset["temporalCoverage"] = facts["date"]
                _compare(page.structured[0], dataset, filename + "/Dataset", errors)
            elif page.structured and page.structured[0].get("description") != description:
                errors.append(filename + ": structured description differs from statistics")
            for graph in page.graphs:
                if "data-bar" in graph:
                    value = _resolve(facts, graph["data-bar"])
                    # Maximum is across the same eight cheapest demand regions.
                    regions = [(i, r) for i, r in enumerate(data["statistics"]["regions"]) if r["classification"] == "standard" and r["allocation"] == "whole"]
                    regions.sort(key=lambda p: (p[1]["on_demand_min"] is None, p[1]["on_demand_min"] or math.inf, p[1]["region"]))
                    largest = max(r[k + "_min"] or 0 for _, r in regions[:8] for k in ("on_demand", "spot"))
                    if graph["width"] != format(value / largest * 450, ".3f"):
                        errors.append(filename + ": region bar width mismatch")
                else:
                    pointer, statistic = graph["data-series"].rsplit("/", 1)
                    series = _resolve(facts, pointer)
                    days = [date.fromisoformat(p["day"]) for p in series]
                    maximum = max(p["max"] for p in series) * 1.1
                    expected = " ".join(format(78 + (d - days[0]).days / (days[-1] - days[0]).days * 760, ".2f") + "," + format(252 - p[statistic] / maximum * 200, ".2f") for d, p in zip(days, series))
                    if graph["points"] != expected:
                        errors.append(filename + ": history polyline mismatch")
                checked += 1
            allowed_times = {r["collected_at"] for r in facts["all_rows"]} | {manifest["generated_at"]}
            if facts["run"]:
                allowed_times.add(facts["run"]["finished_at"])
            for stamp, text in page.times:
                if stamp not in allowed_times or text != stamp[:10]:
                    errors.append(filename + ": observation timestamp mismatch")
            html_pages += 1
    if set(manifest["files"]) != expected_files:
        errors.append("manifest: missing or unexpected audited files")
    if errors:
        raise AuditError("Statistics audit FAILED (" + str(len(errors)) + " mismatches):\n" + "\n".join(errors[:40]))
    return {"status": "passed", "html_pages": html_pages, "json_exports": len(facts["exports"]), "values_checked": checked,
            "observation_day": facts["date"], "basis": "current standard-region whole-GPU USD; separate types; tax excluded/unknown; A-only exports"}


def live_sample(connection, n, *, client=None, rng=None):
    """Random current stored Azure offers, exact SKU/region live re-fetch.

    Historical rows without effective-date provenance compare against collection
    time for source-update detection; the report explicitly records this limit.
    Network/missing/ambiguous records are unverified, never counted as matches.
    """
    from .http import HttpClient, validate_url
    from .providers.azure import normalize_item
    client, rng = client or HttpClient(), rng or random.SystemRandom()
    rows = _rows(connection, "SELECT * FROM offers WHERE provider='azure' AND currency='USD' ORDER BY collected_at DESC")
    keys, latest = set(), []
    for row in rows:
        key = (row["sku"], row["region"], row["price_type"])
        if key not in keys:
            keys.add(key)
            latest.append(row)
    chosen = rng.sample(latest, min(n, len(latest)))
    results = []
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for row in chosen:
        entry = {k: row[k] for k in ("sku", "region", "price_type", "price_per_hour")}
        entry["stored_effective_date_known"] = bool(row.get("effective_start_date"))
        expression = "serviceName eq 'Virtual Machines' and priceType eq 'Consumption' and armSkuName eq '" + row["sku"].replace("'", "''") + "' and armRegionName eq '" + row["region"].replace("'", "''") + "'"
        url = "https://prices.azure.com/api/retail/prices?" + urlencode({"api-version": "2023-01-01-preview", "currencyCode": "'USD'", "$filter": expression})
        matches, visited = [], set()
        try:
            while url:
                validate_url(url)
                if url in visited:
                    raise ValueError("paging cycle")
                visited.add(url)
                payload = client.get_json(url)
                for item in payload["Items"]:
                    normalized = normalize_item(item, source_url=url, collected_at=now)
                    effective = item.get("effectiveStartDate", "")
                    if normalized and (normalized.sku, normalized.region, normalized.price_type) == (row["sku"], row["region"], row["price_type"]) and effective and effective <= now:
                        matches.append((effective, normalized.price_per_hour))
                url = payload.get("NextPageLink")
            if not matches:
                entry["status"] = "unverified_missing"
            else:
                effective = max(d for d, _ in matches)
                prices = {p for d, p in matches if d == effective}
                entry["live_effective_start_date"] = effective
                if len(prices) != 1:
                    entry["status"] = "unverified_ambiguous"
                else:
                    price = prices.pop()
                    entry["live_price_per_hour"] = price
                    baseline = row.get("effective_start_date") or row["collected_at"]
                    newer = datetime.fromisoformat(effective.replace("Z", "+00:00")) > datetime.fromisoformat(baseline.replace("Z", "+00:00"))
                    entry["status"] = "newer_effective_date" if newer else "matched" if abs(price - row["price_per_hour"]) <= 1e-6 else "mismatch"
        except (OSError, ValueError, KeyError, TypeError) as exc:
            entry.update(status="unverified_error", error=type(exc).__name__)
        results.append(entry)
    return {"requested": n, "sampled": len(chosen), "tolerance": 1e-6, "results": results,
            "status": "failed" if any(r["status"] == "mismatch" for r in results) else "incomplete" if any(r["status"].startswith("unverified") for r in results) else "passed"}
