"""Independent build gate. Reads SQLite, never renderer/store/statistics helpers.

Reviewed region/naming policies and translation catalogs are shared. Prices
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
import zlib
from urllib.parse import urlencode

from .i18n import LANGUAGES, load_catalogs
from .regions import REGIONS
from .catalog import AZURE_SKUS
from .links import PROVIDER_ONLY
from .providers.registry import PROVIDERS
from .models import canonical_model, category, popular_rank, POPULAR, CATEGORIES, model_family
from fractions import Fraction

SOURCE = "https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices"
FIELDS = "provider sku gpu_model gpu_count vcpu ram_gb region price_type currency price_per_hour price_per_gpu_hour source_url collected_at effective_start_date price_scope gpu_fraction gpu_model_raw tax_basis is_from_price billing_unit minimum_billing_seconds effective_from effective_to availability availability_observed_at tier export_allowed original_currency original_price fx_rate fx_date fx_source product zone cpu_raw ram_raw vram_raw price_conditions price_per_day source_billing_unit price_basis fx_usd_per_eur fx_original_per_eur".split()


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
    return (row["currency"] == "USD" and row["gpu_count"] is not None and row["gpu_count"] >= 1 and float(row['gpu_count']).is_integer()
            and row.get('gpu_fraction',1) == 1 and not row.get('is_from_price',False)
            and row.get('tax_basis','unknown') != 'included' and row.get('price_scope','instance') in {'instance','per_gpu'}
            and _classification(row["region"]) in {"standard", "unknown_region"} and row["price_type"] in ("on_demand", "spot"))


def _rate(row):
    if row.get('price_scope','instance') != 'instance':
        return row['price_per_hour']
    allocation = row['gpu_count']
    if allocation is None and row.get('gpu_fraction') is not None and row['gpu_fraction'] < 1:
        allocation = row['gpu_fraction']
    return row['price_per_hour'] / allocation if allocation else None


def _allocation(row):
    count, fraction = row['gpu_count'], row['gpu_fraction']
    if (count is not None and (count < 1 or not float(count).is_integer())
            or fraction is not None and fraction < 1):
        return 'fractional'
    return 'whole' if count is not None and fraction == 1 else 'unknown'


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
        for allocation in ("whole", "fractional", "unknown"):
            local = [r for r in rows if r["region"] == name and _allocation(r) == allocation]
            if local:
                entry = {"region": name, "classification": _classification(name), "allocation": allocation, "offers": len(local)}
                for kind in ("on_demand", "spot"):
                    values = [_rate(r) for r in local if _rate(r) is not None and r["price_type"] == kind and r["currency"] == "USD" and r.get("tax_basis") != "included" and not r.get("is_from_price") and r.get("price_scope") != "gpu_component"]
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
    # Decode the on-disk history format independently of the store. It contains
    # daily group minima, not thousands of archived marketplace host listings.
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='history_snapshots'").fetchone():
        for snapshot in _rows(connection, 'SELECT * FROM history_snapshots'):
            archived = json.loads(zlib.decompress(snapshot['payload']))
            if len(archived) != snapshot['row_count'] or any(r['provider'] != snapshot['provider'] or r['day'] != snapshot['day'] for r in archived):
                raise AuditError('history snapshot identity/count mismatch')
            rows.extend(archived)
    for row in rows:
        row['gpu_model'] = canonical_model(row['gpu_model'], row.get('vram_raw'))
        row['region'] = row['region'] or None
        for key in ('is_from_price','export_allowed'):
            row[key] = bool(row[key])
    runs = _rows(connection, "SELECT * FROM collection_runs ORDER BY id")
    run = runs[-1] if runs else None
    providers = {r['provider']:r for r in runs}
    stamp = lambda value: datetime.fromisoformat(value.replace('Z','+00:00'))
    current = []
    for row in rows:
        if row['day'] != day or stamp(row['collected_at'])>stamp(generated) or not _current(row, generated):
            continue
        last = providers.get(row['provider'])
        if last and (last['status']=='failed' or last['started_at'][:10] != day):
            continue
        if last and last['status']=='complete' and not stamp(last['started_at']) <= stamp(row['collected_at']) <= stamp(last['finished_at']):
            continue
        current.append(row)
    current.sort(key=lambda r: (r['currency'], r['price_per_gpu_hour'] is not None, r['price_per_gpu_hour'] or 0,r['provider'],r['region'] or '',r['sku'],r['price_type']))
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
                if row['gpu_model'] == name and row['day'] < day and row['price_type']==kind and _eligible(row) and _current(row,row['day']+'T23:59:59.999999Z') and (not export_only or row['tier']=='A' and row['export_allowed']):
                    grouped.setdefault(row['day'],[]).append(row)
            good = [r for r in active if _eligible(r) and r['price_type']==kind]
            if good:
                grouped[day] = good
            result[kind] = [{'day':d,'currency':'USD','price_type':kind,**_distribution(grouped[d])} for d in sorted(grouped)]
        return result
    observed_rows=[r for r in rows if stamp(r['collected_at'])<=stamp(generated)]
    for name in sorted({r['gpu_model'] for r in observed_rows}):
        offers = [r for r in current if r['gpu_model']==name]
        updated = max((r['collected_at'] for r in offers),default=max(r['collected_at'] for r in observed_rows if r['gpu_model']==name))
        data = {'gpu_model':name,'currency':next(iter({r['currency'] for r in offers})) if len({r['currency'] for r in offers})==1 else 'USD' if not offers else 'MULTI','observation_day':day,'last_updated':updated,
                'generated_at':generated,'source':sorted({PROVIDERS[r['provider']]['source'] for r in offers if r['provider'] in PROVIDERS}) or [SOURCE],'latest_collection':run,
                'offers':[exported(r) for r in offers],'statistics':_statistics(offers),'history':history(name,offers,False)}
        models[name] = data
        permitted = [r for r in offers if r['tier']=='A' and r['export_allowed']]
        history_rows = [r for r in rows if r['gpu_model']==name and r['tier']=='A' and r['export_allowed'] and stamp(r['collected_at'])<=stamp(generated)]
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
        status = ('link_only' if provider['tier']=='D' or provider.get('link_only') else 'disabled') if not provider['enabled'] else ('stale_provider' if last and (last['status']=='failed' or last['started_at'][:10]!=day) else last['status'] if last else 'not_collected')
        coverage.append({'provider':key,'name':provider['name'],'tier':provider['tier'],'status':status,
            'model_count':len({r['gpu_model'] for r in rows if r['provider']==key}),
            'last_success':max((r['finished_at'] for r in runs if r['provider']==key and r['status']=='complete'),default=None)})
    monthly = []
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='monthly_quotes'").fetchone():
        monthly = [json.loads(r[0]) for r in connection.execute('SELECT payload FROM monthly_quotes WHERE collected_at=(SELECT MAX(collected_at) FROM monthly_quotes WHERE collected_at<=?) ORDER BY sku',(stamp(generated).isoformat(timespec='microseconds').replace('+00:00','Z'),))]
    return {'monthly':monthly,'models':models,'exports':exports,'coverage':coverage,'date':day,'built':generated,'run':run,
            'summary':{'models':len({r['gpu_model'] for r in good}),'offers':len(good),'regions':len({r['region'] for r in good}),
                       'providers':len({r['provider'] for r in good}), 'updated':max((r['collected_at'] for r in good), default=None)},
            'all_rows':rows,'last_updated':max((r['collected_at'] for r in observed_rows),default=None),
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
        self.cards, self.card = [], None
        self.monthly_cards = []
        self.groups, self.offer_groups, self.summary_quotes, self.summary_strips = [], [], [], []
        self.links, self.h1, self.search_values, self.breadcrumbs, self.outside_values = [], [], {}, [], []
        self.faq_questions=[]
        self.table_rows,self.current_row=[],None
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag=='tr':self.current_row=[]
        if tag in {'a','link','script'}:self.links.append((tag,a))
        if 'data-model-group' in a:self.groups.append((tag,a))
        if 'data-offer-category' in a:self.offer_groups.append((tag,a))
        if 'data-offer' in a:
            group = next((f[1]['data-model-group'] for f in reversed(self.stack) if 'data-model-group' in f[1]), None)
            offer_group=next((f[1]['data-offer-category'] for f in reversed(self.stack) if 'data-offer-category' in f[1]),None)
            self.card = {'attrs':a, 'roles':{}, 'values':[], 'links':[], 'group':group, 'offer_group':offer_group}
        if self.card is not None and tag == 'a':
            self.card['links'].append(a)
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
            if re.search(r"[0-9$%]", data) and not any("data-stat" in f[1] or "data-faq" in f[1] or "data-search" in f[1] or f[0] == "time" for f in self.stack):
                self.unmarked.append(data.strip())

    def handle_endtag(self, tag):
        if not self.stack:
            return
        kind, a, chunks = self.stack.pop()
        text = ''.join(chunks)
        if kind=='td' and self.current_row is not None:self.current_row.append(text)
        if kind=='tr':
            if self.current_row:self.table_rows.append(self.current_row)
            self.current_row=None
        if kind=='h1':self.h1.append(text)
        if kind=='summary' and any(f[1].get('class')=='faq' for f in self.stack):self.faq_questions.append(text)
        if ('data-search' in a or 'data-faq' in a) and any('hidden' in attrs or 'display:none' in attrs.get('style','').replace(' ','') for attrs in [a]+[f[1] for f in self.stack]):raise AuditError('Search/FAQ answers must be visible')
        if 'data-search' in a:
            key=a['data-search']
            if key in self.search_values:raise AuditError('Duplicate search content marker: '+key)
            self.search_values[key]=text
        if kind=='nav' and a.get('class')=='breadcrumbs':self.breadcrumbs.append(text)
        if 'data-summary-offer' in a:self.summary_quotes.append((a['data-summary-offer'],text))
        if a.get('class')=='summary-strip':self.summary_strips.append(text)
        if 'data-monthly-quote' in a:
            self.monthly_cards.append((a['data-monthly-quote'], text))
        if "data-stat" in a:
            self.values.append((a["data-stat"], a.get("data-format"), text))
            if self.card is None:self.outside_values.append(self.values[-1])
            if self.card is not None:
                self.card['values'].append((a['data-stat'],a.get('data-format'),text))
        if self.card is not None:
            if kind == 'h3':self.card['roles']['offer-model']=text
            if kind == 'a' and a.get('class')=='button':self.card['roles']['cta']=text
            if a.get('class') in {'provider-pill','offer-region','offer-price','per-gpu','checked','price-unit','native-price','hardware','offer-badges','component-note','marketplace-note'}:
                self.card['roles'][a['class']] = text
            if kind == 'article' and 'data-offer' in a:
                self.cards.append(self.card)
                self.card = None
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
    from .search_audit import model_copy
    title,intro,_=model_copy(model,data,tr)
    return title,title+'. '+intro


def _faq_expected(tr, name, data, day):
    stats = data["statistics"]
    demand, spot = (stats["cheapest"][k] for k in ("on_demand", "spot"))
    percent = stats["spot_discount_percent"]
    comparison = tr.text("no_discount") if stats["spot_no_discount"] else tr.text("discount_below", percent=format(percent, ".2f")) if percent is not None else tr.text("not_observed")
    questions = [tr.text("faq_cheapest_q", model=name), tr.text("faq_spot_q"), tr.text("faq_units_q")]
    answers = [tr.text("faq_cheapest_a", demand=_dollars(demand) if demand is not None else tr.text("not_observed"), spot=_dollars(spot) if spot is not None else tr.text("not_observed"), regions=stats["region_count"], offers=stats["offers"], date=day), tr.text("faq_spot_a", comparison=comparison), tr.text("faq_units_a")]
    from .search_audit import model_copy
    return questions+[tr.text('search_trend_q')], answers+[model_copy(name,data,tr)[2]]


def _allowed_literals(tr, facts):
    """Numeric text outside value spans must be a known source identifier or
    an exact localized definition. An extra unmarked '$9.99' fails the audit.
    """
    allowed = {v for v in tr.messages.values() if '{' not in v}
    allowed.update(part.strip() for part in tr.messages['method_api'].split('{source}'))
    # Exact localized referral disclosure, including the contractual 5% and
    # public promo code. Changing either number must still fail the audit.
    partner = json.loads((Path(__file__).resolve().parents[2] / 'config' / 'affiliates.json').read_text(encoding='utf-8'))['surflare']
    allowed.add(tr.text('partner_copy', label=partner['label'], code=partner['promo_code']))
    allowed.add(tr.text('partner_footer', code=partner['promo_code']))
    allowed.add("zhan999 [at] tutamail [dot] com")
    allowed.add(tr.text("basis") + " " + tr.text("maximum_def"))
    allowed.add(tr.text("model_intro", date=facts["date"]))
    for kind in ("on_demand", "spot"):
        allowed.add(tr.text("chart_title", kind=tr.text(kind)))
        allowed.add(tr.text("history_caption", kind=tr.text(kind)))
    for model in facts["models"]:
        from .search_audit import model_copy
        allowed.add(model_copy(model,facts['models'][model],tr)[0])
        allowed.update((model, tr.text("faq_heading", model=model), tr.text("faq_cheapest_q", model=model), model + " / " + tr.text("on_demand_from")))
        allowed.add(tr.text('see_model',model=model))
        stats = facts["models"][model]["statistics"]
        count = tr.text("offer_count_one" if stats["offers"] == 1 else "offer_count_other", count="|VALUE|")
        regions = tr.text("region_count_one" if stats["region_count"] == 1 else "region_count_other", count="|VALUE|")
        text = tr.text("observed_summary", model=model, date="|VALUE|", count=count, regions=regions)
        allowed.update(part.strip() for part in text.split("|VALUE|"))
    for row in facts["all_rows"]:
        allowed.update((row["sku"], row["region"]))
        allowed.update(tr.text(k) + ": " + (row["region"] or tr.text("unknown_region")) for k in ("on_demand", "spot"))
        if _allocation(row) == 'fractional':
            allowed.add(tr.text("fraction_note", share=str(Fraction(min(v for v in (row["gpu_count"],row["gpu_fraction"]) if v is not None)).limit_denominator(12))))
    allowed.update(spec.source_url.rsplit('/', 1)[-1] for spec in AZURE_SKUS.values())
    allowed.update(' / '.join(provider["gpu_models"]) for provider in PROVIDER_ONLY)
    # Home always presents these KPI labels even if not yet tracked.
    allowed.update(model + " / " + tr.text("on_demand_from") for model in ("H100 SXM 80GB", "H200 141GB", "A100 80GB"))
    return allowed


def _audit_summary(page, entries, tr, facts, filename, errors):
    """Re-select each quote from eligible SQL rows; verify complete visible copy.

    Both the full text and the required numeric pointers must agree. Removing a
    marker, changing provider/type/host, or substituting a native/promo offer
    cannot make the strip pass merely by displaying a plausible dollar value.
    """
    good=[(p,r) for p,r in entries if _eligible(r)]
    quotes=[];required=set();parts=[]
    for family in ('H100','A100','RTX 4090'):
        local=[e for e in good if model_family(e[1]['gpu_model'])==family and popular_rank(e[1]['gpu_model'])<len(POPULAR)]
        if not local:continue
        p,row=min(local,key=lambda e:(_rate(e[1]),e[1]['provider'],e[1]['region'] or '',e[0]))
        region=tr.text('host_region',region=row['region']) if row['provider']=='clore' and row['region'] else row['region'] or tr.text('unknown_region')
        qualifier=' · '+tr.text('tier_b' if row['tier']=='B' else 'tier_c') if row['tier'] in {'B','C'} else ''
        text=tr.text('summary_from',model=row['gpu_model'],price=_dollars(_rate(row)))+' ('+tr.text(row['price_type'])+' · '+PROVIDERS[row['provider']]['name']+' · '+region+qualifier+')'
        quotes.append((p,text));parts.append(text)
        required.update(((p+'/gpu_model','text'),(p+'/price_per_gpu_hour','money')))
        if row['region']:required.add((p+'/region','text'))
    text=' • '.join(parts)+tr.text('summary_counts',offers=len(good),providers=len({r['provider'] for _,r in good}))
    required.update((('/summary/offers','text'),('/summary/providers','text')))
    if good:
        text+=' · '+tr.text('summary_updated',time=datetime.fromisoformat(facts['summary']['updated'].replace('Z','+00:00')).strftime('%H:%M UTC'))
        required.add(('/summary/updated','clock'))
    if any(r['provider']=='clore' for _,r in good):
        text+=tr.text('community_marketplace')+' · '+tr.text('clore_price_note')+' · '+tr.text('tax_unknown')
    if page.summary_quotes!=quotes or page.summary_strips!=[text]:
        errors.append(filename+': summary strip selection/price/type/provider/host/count/freshness mismatch')
    if not required<={(p,f) for p,f,_ in page.values}:
        errors.append(filename+': missing summary numeric audit marker')


def _audit_cards(page, name, tr, facts, filename, errors, entries_override=None):
    """Independently reconstruct card selection, scopes, labels and default order.

    The renderer is not an oracle: these identities and amounts come from SQL.
    Native currencies and component rates must never be sorted as USD totals.
    """
    entries=[]
    for model,data in facts['models'].items():
        if name is not None and name != model:continue
        stem='/models/'+model.replace('~','~0').replace('/','~1')+'/offers/'
        entries.extend((stem+str(i),row) for i,row in enumerate(data['offers']))
    fractional=lambda r:_allocation(r)=='fractional'
    order=lambda item:(item[1]['currency']!='USD' or item[1]['price_scope']=='gpu_component' or _rate(item[1]) is None,item[1]['currency'],_rate(item[1]) if _rate(item[1]) is not None else item[1]['price_per_hour'],item[0])
    if entries_override is not None:entries=entries_override
    elif name in facts['models']:entries=sorted(entries,key=order)[:12]
    expected=[]
    if name is None:
        minima=[]
        for model in facts['models']:
            local=[entry for entry in entries if entry[1]['gpu_model']==model and _eligible(entry[1])]
            if local:minima.append(min(local,key=lambda e:(_rate(e[1]),e[1]['provider'],e[1]['region'] or '',e[0])))
        def headline_key(entry):
            row=entry[1];model=row['gpu_model'];rank=popular_rank(model)
            capacity=re.search(r'(\d+(?:\.\d+)?)GB\b',model,re.I)
            small=category(model)=='consumer' and (not capacity or float(capacity.group(1))<12)
            return (0,rank,_rate(row),model) if rank<len(POPULAR) else (1,CATEGORIES.index(category(model)),small,_rate(row),model)
        expected.extend((p,r,'cheapest') for p,r in sorted(minima,key=headline_key))
        canonical_names=[canonical_model(c['attrs'].get('data-gpu-model','')) for c in page.cards if c['attrs'].get('data-list')=='cheapest']
        if len(canonical_names)!=len(set(canonical_names)):
            errors.append(filename+': duplicate canonical headline model')
        group_keys=[]
        for _,row,group in expected:
            if group!='cheapest':continue
            key='popular' if popular_rank(row['gpu_model'])<len(POPULAR) else category(row['gpu_model'])
            if key not in group_keys:group_keys.append(key)
        if [a.get('data-model-group') for _,a in page.groups]!=group_keys:
            errors.append(filename+': GPU category/popular group order mismatch')
        for tag,a in page.groups:
            if a['data-model-group']=='legacy' and (tag!='details' or 'open' in a):
                errors.append(filename+': legacy/mining group must be collapsed details')
        _audit_summary(page,entries,tr,facts,filename,errors)
    fraction_budget=min(20,sum(fractional(r) for _,r in entries)) if name is None else None
    for fraction in (False,True):
        local=sorted((e for e in entries if fractional(e[1])==fraction),key=order)
        if fraction_budget is not None:
            local=local[:fraction_budget if fraction else 100-fraction_budget]
            def category_key(entry):
                model=entry[1]['gpu_model'];key=category(model)
                capacity=re.search(r'(\d+(?:\.\d+)?)GB\b',model,re.I)
                small=key=='consumer' and (not capacity or float(capacity.group(1))<12)
                return CATEGORIES.index(key),small
            local.sort(key=category_key)
        expected.extend((p,r,'fractional' if fraction else 'offers') for p,r in local)
    for tag,attrs in page.offer_groups:
        if attrs['data-offer-category']=='legacy' and (tag!='details' or 'open' in attrs):
            errors.append(filename+': legacy offers must be collapsed details')
    if [(c['attrs'].get('data-offer'),c['attrs'].get('data-list')) for c in page.cards] != [(p,g) for p,_,g in expected]:
        errors.append(filename+': offer card selection/order differs from independent eligible population')
    by_pointer={p:r for p,r,_ in expected}
    for card in page.cards:
        a,roles=card['attrs'],card['roles']
        row=by_pointer.get(a.get('data-offer'))
        if row is None:continue
        instance=row['price_per_hour']*((row['gpu_count'] or 1) if row['price_scope']=='per_gpu' else 1)
        provider=PROVIDERS[row['provider']]['name']
        # links.py has the reviewed destination; brand spelling is not inferred.
        from .links import link_for
        link=link_for(row['provider'],row['sku'],row['region'],row['price_type'],model=row['gpu_model'])
        url=next((item for item in card['links'] if item.get('class')=='button'),{})
        if url.get('href')!=link['url'] or url.get('target')!='_blank' or url.get('rel')!=('sponsored nofollow noopener' if link['is_affiliate'] else 'nofollow noopener'):
            errors.append(filename+': offer card destination/relationship mismatch')
        pointer=a['data-offer']
        provider_route=tr.language.path('/providers/'+('clore-ai' if row['provider']=='clore' else row['provider'].replace('_','-'))+'/')
        if not any(link.get('href')==provider_route for link in card['links']):errors.append(filename+': missing card provider profile link')
        count=str(int(row['gpu_count'])) if row['gpu_count'] in (1,2,4) and not fractional(row) else '8+' if row['gpu_count'] is not None and row['gpu_count']>=8 and not fractional(row) else 'other'
        for attr,value in {'data-gpu-model':row['gpu_model'],'data-gpu-count':count,'data-provider':row['provider'],'data-price-type':row['price_type'],'data-region-class':_classification(row['region'])}.items():
            if a.get(attr)!=value:errors.append(filename+': offer card '+attr+' mismatch')
        for attr,value in (('data-sort-gpu',_rate(row)),('data-sort-instance',instance)):
            target=value if value is not None and row['currency']=='USD' and row['price_scope']!='gpu_component' else math.inf
            try:valid=math.isclose(float(a.get(attr,'')),target,rel_tol=1e-12,abs_tol=1e-12)
            except ValueError:valid=False
            if not valid:errors.append(filename+': offer card sort amount mismatch')
        amount=_dollars(instance) if row['currency']=='USD' else format(instance,'g')
        clock=datetime.fromisoformat(row['collected_at'].replace('Z','+00:00')).strftime('%H:%M UTC')
        expected_roles={'provider-pill':provider,'offer-region':row['region'] or tr.text('unknown_region'),
                        'offer-model':row['gpu_model']+' '+(format(row['gpu_count'],'g')+'×' if row['gpu_count'] is not None else tr.text('unknown_allocation')),
                        'cta':tr.text('view_provider',provider=link['provider_name']),
                        'offer-price':row['currency']+' '+amount+'/hr',
                        'per-gpu':(('' if row['currency']=='USD' else row['currency']+' ')+(_dollars(_rate(row)) if row['currency']=='USD' else format(_rate(row),'g'))+' '+tr.text('per_gpu_hour') if _rate(row) is not None else tr.text('unknown_gpu_rate')),
                        'checked':tr.text('checked_at',time=clock),
                        'price-unit':tr.text('scope_gpu_component' if row['price_scope']=='gpu_component' else 'instance_hour')}
        if row['original_currency'] and row['original_currency']!=row['currency']:
            expected_roles['native-price']=tr.text('original_hourly')+': '+row['original_currency']+' '+format(row['original_price'],'g')+'/hr · '+tr.text('scope_'+row['price_scope'])
        for role,value in expected_roles.items():
            if roles.get(role)!=value:errors.append(filename+': offer card '+role+' mismatch')
        hardware=' · '.join(tr.text(label)+': '+format(row[field],'g') for field,label in (('vcpu','vcpu'),('ram_gb','ram')) if row[field] is not None)
        if roles.get('hardware','')!=hardware:errors.append(filename+': hardware must omit unknown fields')
        if a.get('data-list')=='cheapest':
            group='popular' if popular_rank(row['gpu_model'])<len(POPULAR) else category(row['gpu_model'])
            if card['group']!=group:errors.append(filename+': headline in wrong GPU category')
        elif name is None and card['offer_group']!=category(row['gpu_model']):
            errors.append(filename+': index offer in wrong GPU category/disclosure')
        badges=roles.get('offer-badges','')
        tier_key={'A':'official_api','B':'tier_b','C':'tier_c'}[row['tier']]
        for label in ('tax_'+row['tax_basis'],row['price_type'],tier_key):
            if tr.text(label) not in badges:errors.append(filename+': missing offer card '+label+' label')
        if row['provider']=='clore':
            if tr.text('community_marketplace') not in badges or roles.get('marketplace-note') != tr.text('clore_price_note'):
                errors.append(filename+': missing Clore marketplace/stablecoin qualifier')
        if row['provider']=='ai_galaxy' and tr.text('promo_floor_price') not in badges:
            errors.append(filename+': missing promotional floor-price label')
        if _classification(row['region']) in {'edge','restricted'} and tr.text(_classification(row['region'])) not in badges:
            errors.append(filename+': missing excluded-region badge')
        if fractional(row) and tr.text('fractional') not in badges:
            errors.append(filename+': missing fractional badge')
        if row['is_from_price'] and tr.text('from_price') not in badges:
            errors.append(filename+': missing advertised starting-price badge')
        if row['price_scope']=='gpu_component' and roles.get('component-note') != tr.text('gpu_component_note'):
            errors.append(filename+': missing CPU/RAM component-price qualifier')
        present={(p,f) for p,f,_ in card['values']}
        fields={'price_per_hour','price_per_gpu_hour','gpu_count','vcpu','ram_gb','price_conditions','gpu_model_raw','vram_raw'}
        for field in fields:
            if row[field] is not None and not any(p==pointer+'/'+field for p,_ in present):
                errors.append(filename+': missing card source value '+field)
        required={(pointer,'instance-money' if row['currency']=='USD' else 'instance-number'),(pointer+'/collected_at','clock')}
        if not required<=present:errors.append(filename+': missing card price/freshness marker')


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
    errors, checked, html_pages, parsed_main = [], 0, 0, {}
    # Independently verify the new semantic/provenance contract in SQLite.
    for row in facts['all_rows']:
        key = row['provider'] + '/' + row['sku']
        if row['tier'] not in {'A','B','C'} or row['export_allowed'] and row['tier'] != 'A':
            errors.append(key + ': tier/export policy violation')
        if row['billing_unit'] not in {'instance-hour','instance-minute','instance-second','GPU-hour','gpu-hour','GPU-second'}:
            errors.append(key + ': unresolved billing unit (monthly prices forbidden)')
        rate=_rate(row)
        if row['provider']=='clore':
            daily=row.get('price_per_day')
            if (not isinstance(daily,(int,float)) or not math.isfinite(daily) or daily<=0
                    or row.get('source_billing_unit')!='instance-day' or not row.get('price_basis')
                    or row['currency']!='USD' or not row['gpu_count'] or row['gpu_count']<1
                    or not math.isclose(row['price_per_hour'],daily/24,rel_tol=1e-12)
                    or not math.isclose(row['price_per_gpu_hour'],daily/(24*row['gpu_count']),rel_tol=1e-12)):
                errors.append(key + ': Clore day/hour/GPU arithmetic mismatch')
        if (row['price_scope'] not in {'instance','per_gpu','gpu_component'}
                or (rate is None) != (row['price_per_gpu_hour'] is None)
                or rate is not None and not math.isclose(row['price_per_gpu_hour'],rate,rel_tol=1e-9)):
            errors.append(key + ': price scope or per-GPU normalization mismatch')
        if row['original_currency'] and row['original_currency'] != row['currency']:
            if (not row['fx_date'] or row['fx_date'] > row['day'] or row['fx_source'] != 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml'
                    or not row['fx_rate'] or not math.isclose(row['original_price']*row['fx_rate'],row['price_per_hour'],rel_tol=1e-9)):
                errors.append(key + ': invalid FX provenance')
            usd, native = row.get('fx_usd_per_eur'), row.get('fx_original_per_eur')
            if row['provider'] in {'anygpu','ai_galaxy'} or usd is not None or native is not None:
                if (not usd or not native or not math.isfinite(usd) or not math.isfinite(native)
                        or usd<=0 or native<=0 or not math.isclose(row['fx_rate'],usd/native,rel_tol=1e-12)
                        or not math.isclose(row['price_per_hour'],row['original_price']*usd/native,rel_tol=1e-12)):
                    errors.append(key + ': independent FX cross-rate conversion mismatch')
        if row['provider'] in {'anygpu','ai_galaxy'}:
            if row['tier']!='C' or row['export_allowed'] or row['original_currency']!='CNY':
                errors.append(key + ': CNY display-only policy mismatch')
            if row['provider']=='ai_galaxy' and not row['is_from_price']:
                errors.append(key + ': promotional floor must be a from-price')
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
        language_literals = _allowed_literals(tr, facts)
        routes = [(None, "index.html"), ("/methodology", "methodology.html"), ("/404.html", "404.html")]
        routes += [(name, slugs[name] + "/index.html") for name in facts["models"]]
        for name, relative in routes:
            filename = (lang.prefix + "/" if lang.prefix else "") + relative
            expected_files.add(filename)
            raw = (out / filename).read_text(encoding="utf-8")
            page = Page(raw)
            parsed_main[filename]=(raw,page)
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
            if name is None:
                for listing in PROVIDER_ONLY:
                    if sum(tag=='a' and a.get('data-provider-link')==listing['provider'] for tag,a in page.links) != 1:
                        errors.append(filename+': missing or duplicated link-only provider '+listing['provider'])
                expected_monthly = []
                for i, row in enumerate(facts['monthly']):
                    amount, months = row['price_per_month'], row['commitment_months']
                    if (row['provider']!='cloudclusters' or row['tier']!='B' or row['export_allowed']
                            or row['billing_unit']!='instance-month' or row['tax_basis']!='unknown'
                            or row['currency']!='USD' or row['source_url']!='https://www.cloudclusters.io/server/gpu'
                            or row['region']!='USA' or type(amount) not in (int,float)
                            or not math.isfinite(amount) or amount<=0 or type(months) is not int or months<1):
                        errors.append(filename+': invalid monthly quote policy')
                    from .links import link_for
                    link = link_for('cloudclusters','','','')
                    copy = row['gpu_model']+row['sku']+tr.text('monthly_amount',price=_dollars(amount))
                    copy += tr.text('monthly_commitment',months=format(months,'g'))
                    copy += row['region']+' ? '+tr.text('tax_unknown')
                    copy += tr.text('monthly_promotion' if row['is_promotion'] else 'monthly_standard')
                    copy += row['collected_at'][:10]+' ? '+tr.text('stale_provider' if row['collected_at'][:10]!=facts['date'] else 'complete')
                    copy += tr.text(link['label'],provider=link['provider_name'])
                    expected_monthly.append((str(i),copy))
                    required = {('/monthly/'+str(i)+'/'+k, f) for k,f in (
                        ('gpu_model','text'),('sku','text'),('price_per_month','money'),
                        ('commitment_months','number'),('region','text'),('collected_at','date'))}
                    if not required.issubset({(p,f) for p,f,v in page.values}):
                        errors.append(filename+': monthly quote pointers missing')
                if page.monthly_cards != expected_monthly:
                    errors.append(filename+': monthly quote display or commitment mismatch')
            for tag, attrs in page.links:
                if tag == 'a' and 'data-provider-link' in attrs:
                    from .links import link_for
                    key=attrs['data-provider-link']
                    if key in {'novita','gpuoncloud','massedcompute','cloudclusters','hyperbolic'}:
                        link=link_for(key,'','','')
                        rel='sponsored nofollow noopener' if link['is_affiliate'] else 'nofollow noopener'
                        if attrs.get('href')!=link['url'] or attrs.get('rel')!=rel:
                            errors.append(filename+': affiliate destination or disclosure mismatch')
            for pointer, fmt, actual in page.values:
                try:
                    value = _resolve(page_facts, pointer)
                    expected = _dollars(value) if fmt == "money" else format(value, ".2f") + "%" if fmt == "percent" else format(value, "g") if fmt == "number" else value[:10] if fmt == "date" else str(value)
                    if fmt.startswith('instance-'):
                        amount=value['price_per_hour']*((value['gpu_count'] or 1) if value['price_scope']=='per_gpu' else 1)
                        expected=_dollars(amount) if fmt=='instance-money' else format(amount,'g')
                    elif fmt=='clock':
                        expected=datetime.fromisoformat(value.replace('Z','+00:00')).strftime('%H:%M UTC')
                    elif fmt=='vram':
                        match=re.search(r'\b\d+(?:\.\d+)?\s*(?:GB|GiB)\b',value,re.I)
                        expected=match.group() if match else '—'
                    if fmt in {"history-min", "history-median", "history-max"}:
                        statistic = fmt.removeprefix('history-')
                        label = tr.text({'min': 'minimum', 'median': 'median', 'max': 'maximum'}[statistic])
                        expected = value['day'] + ': ' + label + ' ' + _dollars(value[statistic])
                        if statistic == 'median':
                            expected += ' (n=' + str(value['count']) + ')'
                    if actual != expected:
                        errors.append(f"{filename} {pointer}: expected {expected!r}, found {actual!r}")
                except (KeyError, IndexError, TypeError, ValueError):
                    errors.append(filename + ": invalid statistic pointer " + pointer)
                checked += 1
            allowed = set(language_literals)
            if data:
                allowed.update(tr.text('offer_page',model=name,number=i+1) for i in range((len(data['offers'])+11)//12))
            from .search_specs import PAIRS
            allowed.update(tr.text('compare_label',left=a,right=b) for a,b in PAIRS.values())
            for text in set(page.unmarked):
                if text not in allowed:
                    errors.append(filename + ": unaccounted numeric text " + repr(text))
            present = {p for p, _, _ in page.values}
            _audit_cards(page, name, tr, facts, filename, errors)
            if data:
                main_pointers={c['attrs']['data-offer'] for c in page.cards}
                for i, row in enumerate(data["offers"]):
                    prefix = "/models/" + name.replace("~", "~0").replace("/", "~1") + "/offers/" + str(i) + "/"
                    if prefix[:-1] not in main_pointers:continue
                    for field in ("price_per_hour", "price_per_gpu_hour", "gpu_count", "vcpu", "ram_gb"):
                        if row[field] is None:
                            continue
                        if prefix + field not in present:
                            errors.append(filename + ": missing offer value " + prefix + field)
                if data["offers"]:
                    questions, answers = _faq_expected(tr, name, data, facts["date"])
                    if page.faqs != dict(enumerate(answers)) or page.faq_questions!=questions:
                        errors.append(filename + ": visible FAQ differs from independent statistics")
                    faq = next((d for d in page.structured if d.get("@type") == "FAQPage"), {})
                    entities = [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in zip(questions, answers)]
                    _compare(faq.get("mainEntity"), entities, filename + "/FAQPage", errors)
                export=facts['exports'].get(name)
                dataset = {"@context": "https://schema.org", "@type": "Dataset",
                           "@id": manifest["site_base_url"] + lang.path('/' + slugs[name] + '/') + "#dataset",
                           "name": tr.text("dataset_name", model=name), "description": description,
                           "url": manifest["site_base_url"] + lang.path('/' + slugs[name] + '/'),
                           "isBasedOn": export["source"] if export else [], "inLanguage": lang.code,
                           "spatialCoverage": [{"@type": "Place", "name": r or tr.text('unknown_region')} for r in sorted({r["region"] for r in export["offers"]} if export else set(), key=lambda r: r or "")],
                           "variableMeasured": {"@type": "PropertyValue", "name": "price_per_gpu_hour", "unitText": "USD/GPU-hour"},
                           }
                if name in facts['exports']:
                    dataset['distribution']={'@type':'DataDownload','encodingFormat':'application/json','contentUrl':manifest['site_base_url']+'/data/'+slugs[name]+'.json'}
                    from .search_audit import model_copy
                    dataset['description']=tr.text('dataset_name',model=name)+'. '+model_copy(name,export,tr)[1]
                    currencies=sorted({r['currency'] for r in export['offers']}) or [export['currency']]
                    variables=[{'@type':'PropertyValue','name':'price_per_gpu_hour','unitText':c+'/GPU-hour'} for c in currencies]
                    dataset['variableMeasured']=variables[0] if len(variables)==1 else variables
                if export and export["last_updated"]:
                    dataset["dateModified"] = export["last_updated"]
                if export and export["offers"]:
                    dataset["temporalCoverage"] = facts["date"]
                if not export:
                    dataset={'@context':'https://schema.org','@type':'WebPage','name':title,'description':description,'url':manifest['site_base_url']+lang.path('/'+slugs[name]+'/'),'inLanguage':lang.code}
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
                try:clock=datetime.fromisoformat(stamp.replace('Z','+00:00')).strftime('%H:%M UTC')
                except ValueError:
                    errors.append(filename + ': invalid observation timestamp')
                    continue
                if stamp not in allowed_times or text not in {stamp[:10],clock}:
                    errors.append(filename + ": observation timestamp mismatch")
            html_pages += 1
    from .search_audit import audit_search
    extra_files, seo_counts = audit_search(facts, manifest, out, errors, parsed_main=parsed_main)
    expected_files.update(extra_files)
    html_pages+=seo_counts.pop('extra_html_pages')
    checked+=seo_counts.pop('additional_values_checked')
    if set(manifest["files"]) != expected_files:
        errors.append("manifest: missing or unexpected audited files")
    if errors:
        raise AuditError("Statistics audit FAILED (" + str(len(errors)) + " mismatches):\n" + "\n".join(errors[:40]))
    return {"status": "passed", "html_pages": html_pages, "json_exports": len(facts["exports"]), "values_checked": checked, **seo_counts,
            "observation_day": facts["date"], "basis": "current standard/unspecified-region whole-GPU USD; separate types; tax excluded/unknown; A-only exports"}


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
