"""Accessible HTML, inert JSON-LD and exports built only from stored observations."""

from datetime import date, datetime, timezone
from html import escape
import json
import logging
import os
from pathlib import Path
import re
from statistics import median
from string import Template
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from .catalog import AZURE_SKUS
from .i18n import LANGUAGES, Catalog, load_catalogs
from .providers.azure import SOURCE_DOC
from .schema import Offer, parse_utc, utc_now
from .store import PriceStore, ROOT
from .regions import eligible, region_class, REGION_SOURCES
from .links import link_for, PROVIDER_ONLY, PROVIDER_LINKS
from .monthly import monthly_quotes
from .providers.registry import PROVIDERS
from .partners import surflare
from .models import category, popular_rank, headline_order, POPULAR, CATEGORIES, model_family
from . import search
from .search_specs import PAIRS, REFERENCES, SPECS

DEFAULT_OUT = ROOT / "site_i18n_preview"
DEFAULT_SITE_BASE_URL = "https://example.pages.dev"
LOG = logging.getLogger(__name__)
CSS = Path(__file__).with_name('design.css').read_text(encoding='utf-8')
PAGE = Template("""<!DOCTYPE html>
<html lang="$lang" dir="ltr"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="description" content="$description"/><title>$page_title</title>
<link rel="canonical" href="$canonical"/>
$alternates<meta property="og:type" content="website"/><meta property="og:site_name" content="$site_name"/>
<meta property="og:title" content="$title"/><meta property="og:description" content="$description"/><meta property="og:url" content="$canonical"/>
<meta name="theme-color" content="#185bc0"/><link rel="icon" href="$origin/favicon.svg" type="image/svg+xml"/><meta property="og:image" content="$social_image"/>
<meta property="og:image:alt" content="$title"/><meta name="twitter:image" content="$social_image"/><meta name="twitter:image:alt" content="$title"/>
<meta name="twitter:card" content="$social_card"/><meta name="twitter:title" content="$title"/><meta name="twitter:description" content="$description"/>
$robots$structured<style>$css</style></head>
<body><a class="skip" href="#main">$skip</a><header><a class="brand" href="$home">$site_name</a><nav aria-label="$navigation"><a href="$home">$nav_home</a><a href="$models_path">$nav_models</a><a href="$method_path">$method_label</a></nav>$switcher</header>
<main id="main">$placeholder$body
<!-- Future affiliate link slot: intentionally empty until reviewed and disclosed. -->
<div class="affiliate-slot" data-slot="future-affiliate"></div></main>
<footer><div class="footer-grid"><p>$updated<br/>$generated</p><p>$disclosure</p></div>
<p>$footer_source</p>$partner_footer<p id="contact">$contact_label <span>&#122;&#104;&#97;&#110;&#57;&#57;&#57; [at] &#116;&#117;&#116;&#97;&#109;&#97;&#105;&#108; [dot] &#99;&#111;&#109;</span> &middot; <a href="$method_path">$method_label</a></p></footer></body></html>
""")


def model_slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.casefold()).strip("-") or "gpu"


def model_slugs(models) -> dict[str, str]:
    """Stable for an identical model set regardless of input order; no hash tails.

    Sorted model names claim their natural stem first. Numeric suffixes skip all
    natural stems and fixed site routes, so a collision cannot steal another
    model's unambiguous URL (e.g. 'GPU 2' alongside two variants of 'GPU').
    """
    ordered = sorted(set(models), key=lambda model: (model.casefold(), model))
    reserved = {"data", "index", "methodology", "404", "robots", "sitemap", "providers", "compare", "social"} | {lang.prefix for lang in LANGUAGES if lang.prefix}
    natural = {model_slug(model) for model in ordered}
    used, result = set(reserved), {}
    for model in ordered:
        stem = model_slug(model)
        slug, number = stem, 2
        while slug in used or (slug != stem and slug in natural):
            slug = f"{stem}-{number}"
            number += 1
        result[model] = slug
        used.add(slug)
    return result


def site_origin(value: str | None = None) -> str:
    value = value if value is not None else os.environ.get("SITE_BASE_URL", DEFAULT_SITE_BASE_URL)
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == "https" and parts.hostname and parts.port in (None, 443)
                 and not parts.username and not parts.password and parts.path in ("", "/")
                 and not parts.query and not parts.fragment and not any(c.isspace() for c in value)
                 and not any(c in value for c in '<>"\\'))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("SITE_BASE_URL must be an HTTPS origin without credentials, path, query or fragment")
    return value.rstrip("/")


def _timestamp(value: str | None, tr: Catalog, pointer=None) -> str:
    audit = ' data-stat="' + escape(pointer, quote=True) + '" data-format="date"' if pointer else ''
    return f'<time datetime="{escape(value, quote=True)}"{audit}>{escape(value[:10])}</time>' if value else tr.html("no_observations")


def _alternates(origin: str, route: str) -> dict[str, str]:
    return {**{lang.code: origin + lang.path(route) for lang in LANGUAGES},
            "x-default": origin + LANGUAGES[0].path(route)}


def _language_links(tr: Catalog, route: str) -> str:
    return '<details class="language-menu"><summary>' + tr.html('languages') + ' / ' + escape(tr.language.name) + '</summary><nav class="languages" aria-label="' + tr.html("languages") + '">' + ''.join(
        '<a href="' + escape(lang.path(route), quote=True) + '" lang="' + lang.code
        + '" hreflang="' + lang.code + '"' + (' aria-current="page"' if lang == tr.language else '')
        + '>' + escape(lang.name) + '</a>' for lang in LANGUAGES) + '</nav></details>'


def _page(title, body, updated, built, *, origin, route, tr, description, structured=None, noindex=False, social_image=None, crumbs=None):
    path = tr.language.path(route)
    structured = list(structured) if isinstance(structured, list) else [structured] if structured else []
    crumbs = crumbs or [(tr.text('nav_home'), '/')]+([(title,route)] if route!='/' else [])
    trail, breadcrumb = search.breadcrumb(crumbs, tr, origin, route)
    structured.append(breadcrumb)
    body = trail + body
    # HTML escaping corrupts JSON strings; JSON escapes keep it parseable and
    # prevent model/region text from closing the inert application/ld+json block.
    return PAGE.substitute(origin=origin, social_image=escape(origin+(social_image or '/og.svg'),quote=True), social_card='summary_large_image' if social_image else 'summary', nav_home=tr.html("nav_home"), nav_models=tr.html("nav_models"), models_path=tr.language.path()+"#models", disclosure=tr.html("disclosure"), title=escape(title), page_title=tr.html("page_title", title=title, site_name=tr.text("site_name")),
                           lang=tr.language.code, site_name=tr.html("site_name"), description=escape(description, quote=True),
                           canonical=escape(origin + path, quote=True),
                           alternates=''.join('<link rel="alternate" hreflang="' + code + '" href="'
                                              + escape(url, quote=True) + '"/>\n' for code, url in _alternates(origin, route).items()),
                           skip=tr.html("skip"), navigation=tr.html("navigation"), home=tr.language.path(),
                           method_path=tr.language.path("/methodology"), method_label=tr.html("methodology"),
                           switcher=_language_links(tr, route), contact_label=tr.html("contact"),
                           robots='<meta name="robots" content="noindex"/>' if noindex else "",
                           structured=''.join('<script type="application/ld+json">' + json.dumps(item, ensure_ascii=True, allow_nan=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026') + '</script>' for item in (structured if isinstance(structured, list) else [structured])) if structured else '',
                           placeholder=('<p class="notice">' + tr.rich("placeholder", url='<code>' + DEFAULT_SITE_BASE_URL + '</code>') + '</p>') if origin == DEFAULT_SITE_BASE_URL else "",
                           body=body, updated=tr.rich("updated", date=_timestamp(updated, tr, "/page_updated")),
                           generated=tr.rich("generated", date=_timestamp(built, tr, "/built")), css=CSS,
                           partner_footer=_partner(tr, compact=True),
                           footer_source=tr.rich("footer_source", source='<a href="' + SOURCE_DOC + '">' + tr.html("source_name") + '</a>'))


def _partner(tr, *, compact=False):
    partner = surflare()
    link = ('<a href="' + escape(partner['url'], quote=True)
            + '" target="_blank" rel="sponsored nofollow noopener">'
            + escape(partner['label']) + '</a>')
    if compact:
        return ('<p class="partner-footer">' + tr.html('partner_heading') + ': ' + link
                + ' <span>' + tr.html('partner_footer', code=partner['promo_code']) + '</span></p>')
    return ('<aside class="partner-card" aria-labelledby="partner-heading"><h2 id="partner-heading">'
            + tr.html('partner_heading') + '</h2><p>' + link + '</p><p class="partner-disclosure">'
            + tr.html('partner_copy', label=partner['label'], code=partner['promo_code']) + '</p></aside>')


def model_statistics(offers, *, at=None) -> dict:
    """Whole GPUs in standard or unspecified regions drive comparisons."""
    offers = [o for o in offers if o.current_at(at or o.collected_at)]
    selected = [o for o in offers if eligible(o)]
    distributions = {}
    for kind in ("on_demand", "spot"):
        values = [o.price_per_gpu_hour for o in selected if o.price_type == kind]
        distributions[kind] = ({"min": min(values), "median": median(values), "max": max(values), "count": len(values)} if values else None)
    cheapest = {k: v["min"] if v else None for k, v in distributions.items()}
    regions = []
    for region in sorted({o.region for o in offers}, key=lambda r: r or ""):
        for allocation in ("whole", "fractional", "unknown"):
            local = [o for o in offers if o.region == region and _allocation(o.to_dict()) == allocation]
            if not local:
                continue
            regions.append({"region": region, "classification": region_class(region), "allocation": allocation, "offers": len(local),
                            **{k + "_min": min((o.price_per_gpu_hour for o in local if o.price_per_gpu_hour is not None and o.price_type == k and o.currency == "USD" and o.tax_basis != "included" and not o.is_from_price and o.price_scope != "gpu_component"), default=None) for k in cheapest}})
    demand, spot = cheapest["on_demand"], cheapest["spot"]
    comparable = demand is not None and spot is not None
    return {"offers": len(selected), "all_offers": len(offers), "region_count": len({o.region for o in selected}),
            "all_region_count": len({o.region for o in offers}), "cheapest": cheapest, "distributions": distributions,
            "spot_discount_percent": (1 - spot / demand) * 100 if comparable and spot < demand else None,
            "spot_no_discount": comparable and spot >= demand, "regions": regions}


def _money(value):
    return f"${value:.2f}" if value >= 1 else f"${value:.3f}"


def _pointer(*parts):
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _stat(path, value, fmt="money", tag="span"):
    if value is None:
        return "?"
    if fmt.startswith("instance-"):
        amount = value['price_per_hour'] * ((value['gpu_count'] or 1) if value['price_scope'] == 'per_gpu' else 1)
        formatted = _money(amount) if fmt == 'instance-money' else f'{amount:g}'
    elif fmt == 'date':
        formatted = value[:10]
    elif fmt == 'clock':
        formatted = parse_utc(value).strftime('%H:%M UTC')
    elif fmt == 'vram':
        match = re.search(r'\b\d+(?:\.\d+)?\s*(?:GB|GiB)\b', value, re.I)
        formatted = match.group() if match else '—'
    else:
        formatted = _money(value) if fmt == "money" else f"{value:.2f}%" if fmt == "percent" else f"{value:g}" if fmt == "number" else str(value)
    return '<' + tag + ' data-stat="' + escape(path, quote=True) + '" data-format="' + fmt + '">' + escape(formatted) + '</' + tag + '>'


def _value(path, value, tr, fmt="money"):
    return _stat(path, value, fmt) if value is not None else tr.html("not_observed")


def _outbound(provider, sku, region, kind, model, tr):
    link = link_for(provider, sku, region, kind, model=model)
    rel = "sponsored nofollow noopener" if link["is_affiliate"] else "nofollow noopener"
    return '<a class="button" data-provider-link="'+escape(provider,quote=True)+'" href="' + escape(link["url"], quote=True) + '" target="_blank" rel="' + rel + '">' + tr.html(link["label"], provider=link['provider_name']) + '</a>'


def _badge(key, tr):
    return '<span class="tag ' + key + '">' + tr.html(key) + '</span>'


def _discount(stats, path, tr):
    if stats["spot_no_discount"]:
        return tr.html("no_discount")
    value = stats["spot_discount_percent"]
    return _stat(path + "/spot_discount_percent", value, "percent") if value is not None else tr.html("not_observed")


def _price_cards(model, stats, tr):
    base = _pointer("models", model, "statistics")
    body = '<div class="grid">'
    for kind in ("on_demand", "spot"):
        p = base + "/distributions/" + kind
        spread = stats["distributions"][kind]
        cell = _value(base + "/cheapest/" + kind, stats["cheapest"][kind], tr)
        body += '<article class="card ' + ('demand' if kind == 'on_demand' else 'spot') + '"><p class="eyebrow">' + tr.html(kind + "_from") + '</p><strong class="metric">' + cell + '</strong><p class="unit">' + tr.html("gpu_hour") + '</p>'
        if spread:
            body += '<p>' + tr.rich("median_n", price=_stat(p + "/median", spread["median"]), n=_stat(p + "/count", spread["count"], "text")) + '</p>'
        body += '</article>'
    body += '</div><p>' + tr.html("spot_discount") + ': ' + _discount(stats, base, tr) + '</p>'
    return body


def _model_content(model, stats, today, tr) -> str:
    base = _pointer("models", model, "statistics")
    body = ('<h2>' + tr.html("glance") + '</h2><p>' + tr.rich("observed_summary", date=_stat("/date", today, "text"), model=escape(model),
        count=tr.text('offer_count_one' if stats['offers']==1 else 'offer_count_other',count=_stat(base+'/offers',stats['offers'],'text')),
        regions=tr.text('region_count_one' if stats['region_count']==1 else 'region_count_other',count=_stat(base+'/region_count',stats['region_count'],'text'))) + '</p>')
    body += '<details class="basis"><summary>' + tr.html('definitions_heading') + '</summary><p>' + tr.html('basis') + ' ' + tr.html('maximum_def') + '</p></details>'
    region_rows = []
    for i, region in enumerate(stats["regions"]):
        path = base + "/regions/" + str(i)
        region_rows.append('<tr>' + _cell(region["region"] or tr.text("unknown_region")) + '<td>' + _badge(region["classification"], tr) + _badge({"whole": "whole_gpu", "fractional": "fractional", "unknown": "unknown_allocation"}[region["allocation"]], tr) + '</td>'
            + '<td class="number">' + _stat(path + "/offers", region["offers"], "text") + '</td>'
            + ''.join('<td class="number">' + _value(path + '/' + k + '_min', region[k + '_min'], tr) + '</td>' for k in ("on_demand", "spot")) + '</tr>')
    return body + _region_bars(model, stats, tr) + '<h2>' + tr.html("regions_heading") + '</h2><p>' + tr.html("region_intro") + '</p>' + _table(
        [tr.text(k) for k in ("region", "allocation", "offers", "cheapest_on_demand", "cheapest_spot")], region_rows, tr.text("regions_caption"))


def _region_bars(model, stats, tr):
    rows = [(i, r) for i, r in enumerate(stats["regions"]) if r["classification"] == "standard" and r["allocation"] == "whole"]
    rows.sort(key=lambda pair: (pair[1]["on_demand_min"] is None, pair[1]["on_demand_min"] or float("inf"), pair[1]["region"]))
    rows = rows[:8]
    if not rows:
        return ""
    maximum = max(r[k + "_min"] or 0 for _, r in rows for k in ("on_demand", "spot"))
    height = 40 + 65 * len(rows)
    parts = ['<h2>' + tr.html("region_chart") + '</h2><p class="legend"><span>' + tr.html("on_demand") + '</span><span class="spot">' + tr.html("spot") + '</span></p>',
        '<svg class="chart" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 ' + str(height) + '" role="img" aria-label="' + tr.html("region_chart") + '"><title>' + tr.html("region_chart") + '</title><desc>' + tr.html("region_chart_desc") + '</desc>']
    for row, (i, r) in enumerate(rows):
        y = 30 + row * 65
        parts.append('<text x="16" y="' + str(y + 17) + '" font-size="13">' + escape(r["region"]) + '</text>')
        for offset, (kind, color) in enumerate((("on_demand", "var(--accent)"), ("spot", "var(--spot)"))):
            value = r[kind + "_min"]
            if value is None:
                continue
            width = value / maximum * 450
            path = _pointer("models", model, "statistics", "regions", i, kind + "_min")
            parts.append(f'<rect x="200" y="{y + offset * 22}" width="{width:.3f}" height="16" rx="3" fill="{color}" data-bar="{escape(path, quote=True)}"><title>' + tr.html(kind) + ': ' + escape(r["region"]) + '</title></rect>')
            parts.append(f'<text x="{210 + width:.3f}" y="{y + offset * 22 + 13}" font-size="13">' + _stat(path, value, tag="tspan") + '</text>')
    return ''.join(parts) + '</svg>'


def _table(headers: list[str], rows: list[str], caption: str) -> str:
    return ('<div class="table-wrap" tabindex="0" role="region" aria-label="' + escape(caption, quote=True) + '"><table><caption>' + escape(caption) + '</caption><thead><tr>'
            + ''.join('<th scope="col">' + escape(h) + '</th>' for h in headers)
            + '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')


def _cell(value, cls=""):
    return f'<td class="{cls}">{escape(str(value))}</td>'


def _original(row, path, tr):
    result = '<br/><small>' + tr.html('billing_unit') + ': ' + tr.html('billing_' + row['billing_unit'].replace('-', '_')) + '</small>'
    if row['minimum_billing_seconds'] is not None:
        result += '<br/><small>'+tr.html('minimum_billing')+': '+_stat(path+'/minimum_billing_seconds',row['minimum_billing_seconds'],'number')+'</small>'
    if row['original_currency'] and row['original_currency'] != row['currency']:
        result += '<br/><small>' + tr.html('original_hourly') + ': ' + escape(row['original_currency']) + ' ' + _stat(path+'/original_price',row['original_price'],'number')
        result += ' · ' + tr.html('fx_rate_label') + ': ' + _stat(path+'/fx_rate',row['fx_rate'],'number')
        result += ' · <a href="'+escape(row['fx_source'],quote=True)+'">ECB</a> ' + _stat(path+'/fx_date',row['fx_date'],'text') + '</small>'
    for key in ('cpu_raw','ram_raw','vram_raw'):
        if row.get(key):
            result += '<br/><small>' + _stat(path+'/'+key,row[key],'text') + '</small>'
    return result


def coverage_data(store, today):
    runs = store.provider_runs()
    rows = store.latest_prices()
    result = []
    for key, provider in PROVIDERS.items():
        run = runs.get(key)
        success = store.connection.execute("SELECT MAX(finished_at) FROM collection_runs WHERE provider=? AND status='complete'",(key,)).fetchone()[0]
        status = ('link_only' if provider['tier']=='D' or provider.get('link_only') else 'disabled') if not provider['enabled'] else ('stale_provider' if run and (run['status']=='failed' or run['started_at'][:10] != today) else run['status'] if run else 'not_collected')
        result.append({'provider':key, 'name':provider['name'], 'tier':provider['tier'], 'status':status,
                       'model_count':len({o.gpu_model for o in rows if o.provider==key}), 'last_success':success})
    return result


def _provider_coverage(data, tr):
    rows = []
    for i, entry in enumerate(data):
        path = '/coverage/'+str(i)
        rows.append('<tr>'+_cell(entry['name'])+'<td>'+_badge(_tier_key(entry['tier']),tr)+'</td><td>'+_stat(path+'/model_count',entry['model_count'],'text')+'</td><td>'+(_stat(path+'/last_success',entry['last_success'],'text') if entry['last_success'] else tr.html('not_collected'))+'</td><td>'+tr.html(entry['status'])+'</td></tr>')
    return '<h2>'+tr.html('provider_coverage')+'</h2>'+_table([tr.text(k) for k in ('provider','tier_label','models_tracked','last_success','health_status')],rows,tr.text('provider_coverage'))


def _filters(rows, tr):
    result = '<div class="filters" hidden>'
    for field, values in (('gpu_model', sorted({r['gpu_model'] for r in rows})), ('gpu_count', ['1', '2', '4', '8+']), ('price_type', sorted({r['price_type'] for r in rows})), ('provider', sorted({r['provider'] for r in rows})), ('region_class', sorted({region_class(r['region']) for r in rows}))):
        result += '<label>'+tr.html(field)+' <select data-filter="'+field.replace('_','-')+'"><option value="">'+tr.html('filter_all')+'</option>'
        for value in values:
            label = (PROVIDERS.get(value,{}).get('name',value) if field=='provider' else tr.text('gpu_'+value.removesuffix('+')) if field=='gpu_count' else value if field=='gpu_model' else tr.text(value))
            result += '<option value="'+escape(value,quote=True)+'">'+escape(label)+'</option>'
        result += '</select></label> '
    result += '<label>'+tr.html('sort_by')+' <select data-sort><option value="gpu">'+tr.html('sort_gpu')+'</option><option value="instance">'+tr.html('sort_instance')+'</option></select></label>'
    return result + '<button class="filter-reset" type="button" data-reset>'+tr.html('reset_filters')+'</button></div>'


FILTER_JS = '''<script type="module">
for(const section of document.querySelectorAll('[data-offer-browser]')){
 const filters=[...section.querySelectorAll('[data-filter]')],sort=section.querySelector('[data-sort]');
 const update=()=>{
  for(const list of section.querySelectorAll('[data-offer-list]')){
   const rows=[...list.querySelectorAll('[data-offer]')];
   rows.sort((a,b)=>Number(a.dataset['sort'+(sort.value==='instance'?'Instance':'Gpu')])-Number(b.dataset['sort'+(sort.value==='instance'?'Instance':'Gpu')]));
   for(const row of rows)row.hidden=filters.some(f=>f.value && row.getAttribute('data-'+f.dataset.filter)!==f.value);
   list.append(...rows);
   const heading=list.previousElementSibling;
   if(heading?.tagName==='H3')heading.hidden=!rows.some(r=>!r.hidden);
  }
  for(const group of section.querySelectorAll('[data-offer-category]')){
   const any=[...group.querySelectorAll('[data-offer]')].some(r=>!r.hidden);
   group.hidden=!any;
   if(group.tagName==='DETAILS')group.open=any&&filters.some(f=>f.value);
  }
  section.querySelector('[data-no-results]').hidden=[...section.querySelectorAll('[data-offer]')].some(r=>!r.hidden);
 };
 section.querySelector('.filters').hidden=false;
 for(const control of [...filters,sort])control.addEventListener('change',update);
 section.querySelector('[data-reset]').addEventListener('click',()=>{for(const f of filters)f.value='';sort.value='gpu';update();});
}
</script>'''


def _fractional(row):
    count, fraction = row['gpu_count'], row['gpu_fraction']
    return (count is not None and (count < 1 or not float(count).is_integer())
            or fraction is not None and fraction < 1)


def _allocation(row):
    if _fractional(row):
        return 'fractional'
    return 'whole' if row['gpu_count'] is not None and row['gpu_fraction'] == 1 else 'unknown'


def _tier_key(tier):
    return {'A': 'official_api', 'B': 'tier_b', 'C': 'tier_c', 'D': 'link_only'}[tier]


def _offer_card(row, path, tr, *, group='offers', model_url=None):
    """A buyer-scannable offer. Every source/derived number has an audit pointer."""
    o = row
    fractional = _fractional(o)
    instance = o['price_per_hour'] * ((o['gpu_count'] or 1) if o['price_scope'] == 'per_gpu' else 1)
    comparable = o['currency'] == 'USD' and o['price_scope'] != 'gpu_component'
    count = str(int(o['gpu_count'])) if o['gpu_count'] in (1, 2, 4) and not fractional else '8+' if o['gpu_count'] is not None and o['gpu_count'] >= 8 and not fractional else 'other'
    attrs = {'data-offer':path, 'data-list':group, 'data-gpu-model':o['gpu_model'], 'data-gpu-count':count,
             'data-provider':o['provider'], 'data-price-type':o['price_type'], 'data-region-class':region_class(o['region']),
             'data-sort-gpu':str(o['price_per_gpu_hour']) if comparable and o['price_per_gpu_hour'] is not None else 'Infinity',
             'data-sort-instance':str(instance) if comparable else 'Infinity'}
    body = '<article class="offer-card" '+ ' '.join(k+'="'+escape(v,quote=True)+'"' for k,v in attrs.items())+'><div class="offer-hardware"><h3>'
    allocation = (_stat(path+'/gpu_count',o['gpu_count'],'number')+'×' if o['gpu_count'] is not None else tr.html('unknown_allocation'))
    body += _stat(path+'/gpu_model',o['gpu_model'],'text')+' <small class="allocation">'+allocation+'</small></h3><div class="offer-specs">'
    body += '<div class="offer-badges"><span class="provider-pill"><a href="'+tr.language.path('/providers/'+search.provider_slug(o['provider'])+'/')+'">'+escape(PROVIDERS.get(o['provider'],{}).get('name',o['provider']))+'</a></span>'+_badge(o['price_type'],tr)+_badge('tax_'+o['tax_basis'],tr)
    body += _badge(_tier_key(o['tier']), tr)
    if o['provider']=='clore':body+=_badge('community_marketplace',tr)
    if o['provider']=='ai_galaxy':body+=_badge('promo_floor_price',tr)
    if region_class(o['region']) in {'edge','restricted'}:body+=_badge(region_class(o['region']),tr)
    if o['price_scope'] != 'instance':body+=_badge('scope_'+o['price_scope'],tr)
    if o['is_from_price']:body+=_badge('from_price',tr)
    if fractional:body+=_badge('fractional',tr)
    hardware = []
    if o['vcpu'] is not None:hardware.append(tr.html('vcpu')+': '+_stat(path+'/vcpu',o['vcpu'],'number'))
    if o['ram_gb'] is not None:hardware.append(tr.html('ram')+': '+_stat(path+'/ram_gb',o['ram_gb'],'number'))
    body+='</div>' + ('<p class="hardware">'+' · '.join(hardware)+'</p>' if hardware else '')+'</div><div class="offer-location"><p class="offer-region">'+escape(o['region'] or tr.text('unknown_region'))+'</p>'
    if o['provider']=='clore':body+='<p class="marketplace-note">'+tr.html('clore_price_note')+'</p>'
    body+='</div></div>'
    body+='<div class="offer-cost"><div class="offer-price"><span class="currency">'+escape(o['currency'])+'</span> <strong>'+_stat(path,o,'instance-money' if o['currency']=='USD' else 'instance-number')+'</strong><span class="hour-unit">/hr</span></div>'
    body+='<p class="per-gpu">'+(('' if o['currency']=='USD' else escape(o['currency'])+' ')+_stat(path+'/price_per_gpu_hour',o['price_per_gpu_hour'],'money' if o['currency']=='USD' else 'number')+' '+tr.html('per_gpu_hour') if o['price_per_gpu_hour'] is not None else tr.html('unknown_gpu_rate'))+'</p>'
    body+='<p class="offer-meta"><span class="price-unit">'+tr.html('scope_gpu_component' if o['price_scope']=='gpu_component' else 'instance_hour')+'</span> · <span class="checked">'+tr.rich('checked_at',time='<time datetime="'+escape(o['collected_at'],quote=True)+'">'+_stat(path+'/collected_at',o['collected_at'],'clock')+'</time>')+'</span></p>'
    if o['price_scope']=='gpu_component':body+='<p class="component-note">'+tr.html('gpu_component_note')+'</p>'
    if o['original_currency'] and o['original_currency'] != o['currency']:
        body+='<p class="native-price">'+tr.html('original_hourly')+': '+escape(o['original_currency'])+' '+_stat(path+'/original_price',o['original_price'],'number')+'/hr · '+tr.html('scope_'+o['price_scope'])+'</p>'
    if o['currency'] != 'USD':body+='<p class="unit">'+tr.html('no_usd_conversion')+'</p>'
    body+='</div>'
    body+='<div class="offer-action">'+_outbound(o['provider'],o['sku'],o['region'],o['price_type'],o['gpu_model'],tr)
    if model_url:body+='<a class="model-link" href="'+model_url+'">'+tr.html('see_offers')+'</a>'
    body+='<details class="offer-details"><summary>'+tr.html('offer_details_short')+'</summary><p>'+tr.html('sku')+': '+_stat(path+'/sku',o['sku'],'text')+' · '+tr.html('availability')+': '+tr.html(o['availability'])+' · '+_badge(_tier_key(o['tier']),tr)+'</p>'
    for field, label in (('gpu_model_raw','reported_model'),('vram_raw','reported_vram')):
        if o.get(field):body+='<p>'+tr.html(label)+': '+_stat(path+'/'+field,o[field],'text')+'</p>'
    body+='<p>'+tr.html('sku_vm_hour')+': '+escape(o['currency'])+' '+_stat(path+'/price_per_hour',o['price_per_hour'],'money' if o['currency']=='USD' else 'number')+'/hr · '+tr.html('scope_'+o['price_scope'])+_original(o,path,tr)+'</p>'
    if fractional:
        share=__import__('fractions').Fraction(min(v for v in (o['gpu_count'],o['gpu_fraction']) if v is not None)).limit_denominator(12)
        body+='<p>'+tr.html('fraction_note',share=str(share))+'</p>'
    if o.get('price_conditions'):body+='<p>'+_stat(path+'/price_conditions',o['price_conditions'],'text')+'</p>'
    return body+'<a href="'+escape(o['source_url'],quote=True)+'">'+tr.html('api_record')+'</a></details></div></article>'


def _offer_browser(entries, tr, *, cap=None):
    """entries retain their original model index for independent SQLite audits."""
    body='<section data-offer-browser><h2 id="offers">'+tr.html('offers_caption')+'</h2>'+_filters([o for _,o in entries],tr)
    # Keep fractional offers discoverable even when the whole-GPU list is capped.
    fraction_budget=min(20,sum(_fractional(o) for _,o in entries)) if cap else None
    for fractional in (False,True):
        local=[(p,o) for p,o in entries if _fractional(o)==fractional]
        if not local:continue
        if fractional:body+='<h3>'+tr.html('fraction_heading')+'</h3>'
        local.sort(key=lambda pair:(pair[1]['currency']!='USD' or pair[1]['price_scope']=='gpu_component' or pair[1]['price_per_gpu_hour'] is None,pair[1]['currency'],pair[1]['price_per_gpu_hour'] if pair[1]['price_per_gpu_hour'] is not None else pair[1]['price_per_hour'],pair[0]))
        if cap is not None:
            local=local[:fraction_budget if fractional else cap-fraction_budget]
        if not local:continue
        # Keep the cap's population, but collapse legacy/mining offers on the
        # index too. Model pages continue to show their complete offer lists.
        groups = [(None, local)] if cap is None else [
            (key, [(p,o) for p,o in local if category(o['gpu_model'])==key]) for key in CATEGORIES]
        for key, grouped in groups:
            if not grouped:continue
            if key=='consumer':
                grouped.sort(key=lambda e:_small_consumer(e[1]))
            if key=='legacy':body+='<details class="legacy-offers" data-offer-category="legacy"><summary>'+tr.html('legacy_gpus')+'</summary>'
            elif key:body+='<section data-offer-category="'+key+'"><h3 class="group-heading">'+tr.html(key+'_gpus')+'</h3>'
            # Separate memory bands so filtering/sorting preserves the priority
            # of 12 GB+ models within the consumer category.
            bands = [[e for e in grouped if _small_consumer(e[1])==small] for small in (False,True)] if key=='consumer' else [grouped]
            for band in bands:
                if band:body+='<div class="offer-list" data-offer-list>'+''.join(_offer_card(o,p,tr,group='fractional' if fractional else 'offers') for p,o in band)+'</div>'
            if key:body+='</details>' if key=='legacy' else '</section>'
    return body+'<p class="notice" data-no-results hidden>'+tr.html('no_results')+'</p></section>'+FILTER_JS


def _small_consumer(row):
    capacity=re.search(r'(\d+(?:\.\d+)?)GB\b',row['gpu_model'],re.I)
    return not capacity or float(capacity.group(1))<12


def _chart(series, kind, tr, base):
    if len(series)<2:return '<p class="muted">'+tr.html('history_wait')+'</p>'
    days=[date.fromisoformat(p['day']) for p in series];span=(days[-1]-days[0]).days
    maximum=max(p['max'] for p in series)*1.1
    x=lambda d:78+(d-days[0]).days/span*760
    y=lambda v:252-v/maximum*200
    title=tr.html('chart_title',kind=tr.text(kind))
    parts=['<svg class="chart" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 330" role="img" aria-label="'+title+'"><title>'+title+'</title><desc>'+tr.html('chart_description')+'</desc>']
    for key,color,dash in (('min','var(--spot)',''),('median','var(--accent)','6 3'),('max','#b77620','2 3')):
        points=' '.join(f"{x(d):.2f},{y(p[key]):.2f}" for d,p in zip(days,series))
        parts.append('<polyline fill="none" stroke="'+color+'" stroke-width="2.5" stroke-dasharray="'+dash+'" points="'+points+'" data-series="'+escape(base+'/'+key,quote=True)+'"/>')
        for i,(d,p) in enumerate(zip(days,series)):
            # HTMLParser 3.12.12+ treats title as escapable raw text, including
            # SVG titles. Never nest HTML spans inside a text-only tooltip.
            # The independent audit verifies its date, price and sample count.
            label = (p['day'] + ': ' + tr.text(key if key == 'median' else 'minimum' if key == 'min' else 'maximum')
                     + ' ' + _money(p[key]) + (' (n=' + str(p['count']) + ')' if key == 'median' else ''))
            parts.append(f'<circle cx="{x(d):.2f}" cy="{y(p[key]):.2f}" r="3" fill="{color}">'
                         + '<title data-stat="' + escape(base+'/'+str(i), quote=True) + '" data-format="history-'
                         + key + '">' + escape(label) + '</title></circle>')
    parts+=['<text x="78" y="280" font-size="12">'+_stat(base+'/0/day',series[0]['day'],'text',tag='tspan')+'</text>','<text x="838" y="280" text-anchor="end" font-size="12">'+_stat(base+'/'+str(len(series)-1)+'/day',series[-1]['day'],'text',tag='tspan')+'</text>','<text x="78" y="309" font-size="13">'+tr.html('chart_legend')+'</text></svg>']
    rows=['<tr><td>'+_stat(base+'/'+str(i)+'/day',p['day'],'text')+'</td><td>'+_stat(base+'/'+str(i)+'/count',p['count'],'text')+'</td>'+''.join('<td class="number">'+_stat(base+'/'+str(i)+'/'+k,p[k]) + (' (n='+_stat(base+'/'+str(i)+'/count',p['count'],'text')+')' if k=='median' else '')+'</td>' for k in ('min','median','max'))+'</tr>' for i,p in enumerate(series)]
    return ''.join(parts)+'<details><summary>'+tr.html('history_exact')+'</summary>'+_table([tr.text(k) for k in ('utc_day','offers','minimum','median','maximum')],rows,tr.text('history_caption',kind=tr.text(kind)))+'</details>'


def _coverage(run, tr):
    body = '<p class="notice">' + tr.html("coverage") + '</p>'
    if run:
        body += '<p>' + tr.rich("collection", status='<strong>' + tr.html(run["status"]) + '</strong>',
                               pages=_stat("/run/pages",run["pages"],"text"), count=_stat("/run/records",run["records"],"text"),
                               date=_timestamp(run["finished_at"], tr, "/run/finished_at")) + '</p>'
        if run["status"] != "complete":
            body += '<p class="notice">' + tr.html("partial") + '</p>'
    else:
        body += '<p>' + tr.html("no_collection") + '</p>'
    return body


def _provider_cards(tr):
    body = '<h2>' + tr.html("providers_heading") + '</h2><div class="grid">'
    for listing in PROVIDER_ONLY:
        body += '<article class="card"><h3>' + escape(listing["name"]) + '</h3><p>' + escape(' / '.join(listing["gpu_models"])) + '</p><p>' + tr.html(listing["note"]) + '</p>' + _outbound(listing["provider"], "", "", "", listing["provider"], tr) + '</article>'
    return body + '</div>'


def _monthly_cards(rows, tr, today):
    if not rows:
        return ''
    body = '<h2>'+tr.html('monthly_heading')+'</h2><p>'+tr.html('monthly_policy')+'</p><div class="grid">'
    for i, row in enumerate(rows):
        path = '/monthly/'+str(i)
        value = lambda field, fmt='text': _stat(path+'/'+field, row[field], fmt)
        body += '<article class="card" data-monthly-quote="'+str(i)+'"><h3>'+value('gpu_model')+'</h3><p>'+value('sku')+'</p>'
        body += '<p>'+tr.rich('monthly_amount',price=value('price_per_month','money'))+'</p>'
        body += '<p>'+tr.rich('monthly_commitment',months=value('commitment_months','number'))+'</p>'
        body += '<p>'+value('region')+' ? '+tr.html('tax_unknown')+'</p>'
        body += '<p>'+tr.html('monthly_promotion' if row['is_promotion'] else 'monthly_standard')+'</p>'
        body += '<p>'+value('collected_at','date')+' ? '+tr.html('stale_provider' if row['collected_at'][:10] != today else 'complete')+'</p>'
        body += _outbound(row['provider'],'','','',row['provider'],tr)+'</article>'
    return body+'</div>'


def _summary_strip(entries, tr):
    good = [(p, o) for p, o in entries if eligible(Offer(**o))]
    parts = []
    for family in ('H100', 'A100', 'RTX 4090'):
        local = [(p, o) for p, o in good if model_family(o['gpu_model']) == family
                 and popular_rank(o['gpu_model']) < len(POPULAR)]
        if not local:continue
        p, o = min(local, key=lambda pair:(pair[1]['price_per_gpu_hour'], pair[1]['provider'], pair[1]['region'] or '', pair[0]))
        provider = escape(PROVIDERS[o['provider']]['name'])
        region = _stat(p+'/region',o['region'],'text') if o['region'] else tr.html('unknown_region')
        if o['provider']=='clore' and o['region']:region=tr.rich('host_region',region=region)
        parts.append('<span class="summary-quote" data-summary-offer="'+escape(p,quote=True)+'">'
                     +tr.rich('summary_from',model=_stat(p+'/gpu_model',o['gpu_model'],'text'),price=_stat(p+'/price_per_gpu_hour',o['price_per_gpu_hour']))
                     +' <small>('+tr.html(o['price_type'])+' · '+provider+' · '+region
                     +(' · '+tr.html(_tier_key(o['tier'])) if o['tier'] in {'B','C'} else '')+')</small></span>')
    body = '<aside class="summary-strip" aria-label="'+tr.html('market_summary')+'">'+'<span class="summary-separator"> • </span>'.join(parts)
    body += '<span class="summary-counts">'+tr.rich('summary_counts', offers=_stat('/summary/offers',len(good),'text'),providers=_stat('/summary/providers',len({o['provider'] for _,o in good}),'text'))
    updated = max((o['collected_at'] for _,o in good),default=None)
    if updated:body+=' · '+tr.rich('summary_updated',time=_stat('/summary/updated',updated,'clock'))
    body += '</span>'
    if any(o['provider']=='clore' for _,o in good):body+='<small class="summary-marketplace">'+tr.html('community_marketplace')+' · '+tr.html('clore_price_note')+' · '+tr.html('tax_unknown')+'</small>'
    return body+'</aside>'


def _home(offers, models, slugs, today, coverage, tr, model_data, monthly=()):
    selected = [o for o in offers if eligible(o)]
    active = sorted({o.gpu_model for o in selected})
    body = '<section class="hero"><h1>' + tr.html("home_heading") + '</h1><p>' + tr.html("home_intro") + '</p></section>'
    entries = [( _pointer('models',m,'offers',i),o) for m,data in model_data.items() for i,o in enumerate(data['offers'])]
    eligible_keys = {(o.provider,o.sku,o.region,o.price_type,o.gpu_model) for o in selected}
    cheapest=[]
    for model in active:
        local=[(p,o) for p,o in entries if o['gpu_model']==model and (o['provider'],o['sku'],o['region'],o['price_type'],model) in eligible_keys]
        cheapest.append(min(local,key=lambda pair:(pair[1]['price_per_gpu_hour'],pair[1]['provider'],pair[1]['region'] or '',pair[0])))
    cheapest.sort(key=lambda pair:headline_order(pair[1]))
    body+=_summary_strip(entries,tr)
    body+='<section class="headlines" aria-labelledby="models"><h2 id="models">'+tr.html('cheapest_models')+'</h2>'
    groups = [('popular',[(p,o) for p,o in cheapest if popular_rank(o['gpu_model'])<len(POPULAR)])]
    groups += [(key,[(p,o) for p,o in cheapest if popular_rank(o['gpu_model'])==len(POPULAR) and category(o['gpu_model'])==key]) for key in CATEGORIES]
    other_started = False
    for key, local in groups:
        if not local:continue
        if key!='popular' and not other_started:
            body+='<h3 class="other-heading">'+tr.html('other_gpus')+'</h3>'
            other_started=True
        if key=='legacy':body+='<details class="legacy-gpus" data-model-group="legacy"><summary>'+tr.html('legacy_gpus')+'</summary>'
        else:body+='<section data-model-group="'+key+'"><h3 class="group-heading">'+tr.html('popular_gpus' if key=='popular' else key+'_gpus')+'</h3>'
        body+='<div class="offer-list cheapest-list">'+''.join(_offer_card(o,p,tr,group='cheapest',model_url=tr.language.path('/'+slugs[o['gpu_model']]+'/')) for p,o in local)+'</div>'
        body+='</details>' if key=='legacy' else '</section>'
    body+='</section>'
    if not cheapest:body+='<p class="notice">'+tr.html('empty_today')+'</p>'
    body+=_offer_browser(entries,tr,cap=100)
    if len(entries)>100:body+='<p class="unit">'+tr.html('list_cap')+'</p>'
    body+='<div class="model-directory">'+''.join('<a href="'+tr.language.path('/'+slugs[m]+'/')+'">'+tr.html('see_model',model=m)+'</a>' for m in models)+'</div>'
    # Partners follow both price lists. The footer is rendered unchanged.
    body+=_partner(tr)+_provider_cards(tr)+_monthly_cards(monthly,tr,today)
    summary=[]
    for m in active:
        stats=model_data[m]['statistics'];base=_pointer('models',m,'statistics')
        summary.append('<tr>'+_cell(m)+''.join('<td>'+_value(base+'/cheapest/'+k,stats['cheapest'][k],tr)+' '+tr.html('gpu_hour')+'</td>' for k in ('on_demand','spot'))+'<td>'+_discount(stats,base,tr)+'</td></tr>')
    body+='<details class="comparison"><summary>'+tr.html('how_compare')+'</summary><p>'+tr.html('basis')+'</p><p>'+tr.html('maximum_def')+'</p>'+_table([tr.text(k) for k in ('gpu_model','cheapest_on_demand','cheapest_spot','spot_discount')],summary,tr.text('home_caption'))+coverage+'<p><a href="'+tr.language.path('/methodology')+'">'+tr.html('methodology')+'</a></p></details>'
    numbers=dict(count=tr.quantity('offer_count',len(selected)),models=tr.quantity('model_count',len(active)),regions=tr.quantity('region_count',len({o.region for o in selected})),date=today)
    return body,tr.text('home_title',**numbers),tr.text('home_description',**numbers)


def _faq(model, stats, today, tr, data):
    demand, spot = stats['cheapest']['on_demand'], stats['cheapest']['spot']
    comparison = tr.text('no_discount') if stats['spot_no_discount'] else (tr.text('discount_below',percent=f"{stats['spot_discount_percent']:.2f}") if stats['spot_discount_percent'] is not None else tr.text('not_observed'))
    answers = [tr.text('faq_cheapest_a',demand=_money(demand) if demand is not None else tr.text('not_observed'),spot=_money(spot) if spot is not None else tr.text('not_observed'),regions=stats['region_count'],offers=stats['offers'],date=today),
               tr.text('faq_spot_a',comparison=comparison),tr.text('faq_units_a')]
    questions = [tr.text('faq_cheapest_q',model=model),tr.text('faq_spot_q'),tr.text('faq_units_q')]
    return list(zip(questions+[tr.text('search_trend_q')],answers+[search.trend_text(data,tr)]))


def _model(model, data, today, coverage, tr, origin, route, export_path, export_data):
    current,stats,updated=data['offers'],data['statistics'],data['last_updated']
    base=_pointer('models',model)
    title=search.title(model,data,tr)
    description=title+'. '+search.intro(data,tr)
    body='<section class="hero"><h1>'+escape(title)+'</h1><p data-search="intro">'+escape(search.intro(data,tr))+'</p><p data-search="trend">'+escape(search.trend_text(data,tr))+'</p><p>'+tr.html('search_limits')+'</p></section>'
    if not current:body+='<p class="notice">'+tr.html('stale')+'</p>'
    else:
        body+=_offer_browser(search.model_chunk(data,model),tr)
        body+=search.pagination(model,len(current),route.strip('/'),tr)
        body+='<details class="comparison"><summary>'+tr.html('how_compare')+'</summary>'+_price_cards(model,stats,tr)+'<p>'+tr.html('basis')+'</p>'+coverage+'</details>'
        body+=_model_content(model,stats,today,tr)
    body+='<h2>'+tr.html('history_heading')+'</h2><p>'+tr.html('history_intro')+'</p><p>'+tr.html('history_aggregation_note')+'</p>'
    for kind,series in data['history'].items():
        if series:body+='<h3>'+tr.html(kind)+'</h3>'+_chart(series,kind,tr,base+'/history/'+kind)
    if export_path:
        body+='<p><a href="'+export_path+'">'+tr.html('download')+'</a></p>'
    if export_data is not None:
        currencies=sorted({o['currency'] for o in export_data['offers']}) or [export_data['currency']]
        variables=[{'@type':'PropertyValue','name':'price_per_gpu_hour','unitText':c+'/GPU-hour'} for c in currencies]
        dataset={"@context":"https://schema.org","@type":"Dataset","@id":origin+tr.language.path(route)+'#dataset',"name":tr.text('dataset_name',model=model),"description":tr.text('dataset_name',model=model)+'. '+search.intro(export_data,tr),"url":origin+tr.language.path(route),"isBasedOn":export_data["source"],"inLanguage":tr.language.code,
            "spatialCoverage":[{"@type":"Place","name":r or tr.text('unknown_region')} for r in sorted({r['region'] for r in export_data['offers']},key=lambda r:r or '')],"variableMeasured":variables[0] if len(variables)==1 else variables,
            'distribution':{'@type':'DataDownload','encodingFormat':'application/json','contentUrl':origin+export_path}}
        if export_data['last_updated']:dataset['dateModified']=export_data['last_updated']
        if export_data['offers']:dataset['temporalCoverage']=today
    else:
        dataset={"@context":"https://schema.org","@type":"WebPage","name":title,"description":description,"url":origin+tr.language.path(route),"inLanguage":tr.language.code}
    structured=[dataset]
    if current:
        faqs=_faq(model,stats,today,tr,data)
        body+='<section class="faq"><h2>'+tr.html('faq_heading',model=model)+'</h2>'+''.join('<details><summary>'+escape(q)+'</summary><p data-faq="'+str(i)+'">'+escape(a)+'</p></details>' for i,(q,a) in enumerate(faqs))+'</section>'
        structured.append({"@context":"https://schema.org","@type":"FAQPage","inLanguage":tr.language.code,"url":origin+tr.language.path(route),"mainEntity":[{"@type":"Question","name":q,"acceptedAnswer":{"@type":"Answer","text":a}} for q,a in faqs]})
    return title,description,body,structured


def _provider_page(key, model_data, slugs, today, tr):
    provider=PROVIDERS[key]
    local=[(p,o) for p,o in search.entries(model_data) if o['provider']==key]
    good=[(p,o) for p,o in local if eligible(Offer(**o))]
    title=tr.text('provider_title',provider=provider['name'],date=today)
    intro=tr.text('provider_intro',provider=provider['name'])
    body='<h1>'+escape(title)+'</h1><p>'+escape(intro)+'</p><p>'+_badge(_tier_key(provider['tier']),tr)+' '+tr.html(provider['note'])+'</p>'
    # Numeric prose is audited as a complete sentence against independent SQL.
    body+='<p data-search="provider-counts">'+tr.html('provider_counts',offers=len(local),eligible=len(good),models=len({o['gpu_model'] for _,o in local}))+'</p>'
    body+='<p><a href="'+escape(provider['source'],quote=True)+'">'+tr.html('source')+'</a></p>'
    if key in PROVIDER_LINKS:
        body+=_outbound(key,'',None,'on_demand',None,tr)
    rows=[]
    for model in sorted({o['gpu_model'] for _,o in local}):
        items=[(p,o) for p,o in local if o['gpu_model']==model]
        valid=[(p,o) for p,o in good if o['gpu_model']==model]
        best=min(valid,key=lambda p:p[1]['price_per_gpu_hour']) if valid else None
        rows.append('<tr><td><a href="'+tr.language.path('/'+slugs[model]+'/')+'">'+escape(model)+'</a></td><td data-search="count:'+escape(model,quote=True)+'">'+str(len(items))+'</td><td data-search="eligible:'+escape(model,quote=True)+'">'+str(len(valid))+'</td><td>'+(_stat(best[0]+'/price_per_gpu_hour',best[1]['price_per_gpu_hour'])+' '+tr.html(best[1]['price_type']) if best else tr.html('not_observed'))+'</td></tr>')
    body+='<h2>'+tr.html('provider_cheapest')+'</h2>'+_table([tr.text('gpu_model'),tr.text('offers'),tr.text('eligible_offers'),tr.text('cheapest_gpu_hour')],rows,tr.text('provider_cheapest'))
    body+='<p>'+tr.html('search_limits')+'</p>'
    # Full cards preserve source, availability, original prices and fee labels.
    selected=search.ordered(good)[:3]
    if selected:body+=_offer_browser(selected,tr)
    local_names={o['gpu_model'] for _,o in local}
    pairs=[(slug,pair) for slug,pair in PAIRS.items() if any(local_names.intersection(search.family_models(model_data,f)) for f in pair)]
    body+='<nav><h2>'+tr.html('comparisons_heading')+'</h2>'+''.join('<p><a href="'+tr.language.path('/compare/'+slug+'/')+'">'+tr.html('compare_label',left=pair[0],right=pair[1])+'</a></p>' for slug,pair in pairs)+'</nav>'
    return title,intro,body,len(good)


def _comparison_page(slug, model_data, slugs, today, tr):
    left,right=PAIRS[slug]
    family_names={f:set(search.family_models(model_data,f)) for f in (left,right)}
    title=tr.text('compare_title',left=left,right=right,date=today)
    description=tr.text('compare_intro')
    body='<h1>'+escape(title)+'</h1><p>'+escape(description)+'</p><p>'+tr.html('search_limits')+'</p>'
    rows=[]
    totals=[]
    for family in (left,right):
        names=sorted(family_names[family])
        totals.append(sum(model_data[m]['statistics']['offers'] for m in names))
        for model in names:
            stats=model_data[model]['statistics'];p=_pointer('models',model,'statistics')
            rows.append('<tr><td><a href="'+tr.language.path('/'+slugs[model]+'/')+'">'+escape(model)+'</a></td><td>'+_stat(p+'/offers',stats['offers'],'number')+'</td>'+''.join('<td>'+_value(p+'/cheapest/'+kind,stats['cheapest'][kind],tr)+'</td>' for kind in ('on_demand','spot'))+'</tr>')
    body+=_table([tr.text(k) for k in ('gpu_model','offers','cheapest_on_demand','cheapest_spot')],rows,tr.text('spread_caption'))
    spec_rows=[]
    for family in (left,right):
        model=REFERENCES[family];spec=SPECS[model];p=_pointer('specs',model)
        spec_rows.append('<tr>'+_cell(model)+''.join('<td>'+_stat(p+'/'+k,spec[k],'number')+'</td>' for k in ('vram','bandwidth','fp16','bf16'))+'<td>'+tr.html(spec['condition'])+'</td></tr>')
    body+='<h2>'+tr.html('reference_specs')+'</h2><p>'+tr.html('spec_note')+'</p>'+_table([tr.text(k) for k in ('gpu_model','spec_vram','spec_bandwidth','spec_fp16','spec_bf16','spec_conditions')],spec_rows,tr.text('reference_specs'))
    body+='<h2>'+tr.html('spec_sources')+'</h2><ul>'+''.join('<li><a href="'+SPECS[REFERENCES[f]]['source']+'">'+escape(REFERENCES[f])+'</a></li>' for f in (left,right))+'</ul>'
    local=[(p,o) for p,o in search.entries(model_data) if o['gpu_model'] in family_names[left]|family_names[right] and eligible(Offer(**o))]
    selected=[]
    for family in (left,right):
        selected+=search.ordered([(p,o) for p,o in local if o['gpu_model'] in family_names[family]])[:1]
    if selected:body+=_offer_browser(selected,tr)
    return title,description,body,min(totals)


def _methodology(coverage, tr):
    source = '<a href="' + SOURCE_DOC + '">' + tr.html("source_name") + '</a>'
    body = '<h1>' + tr.html("methodology") + '</h1>' + coverage + '<p>' + tr.rich("method_api", source=source) + '</p>'
    body += ''.join('<p>' + tr.html(key) + '</p>' for key in (
        "basis", "maximum_def", "method_regions", "method_rounding", "method_cadence", "method_audit", "method_filters", "method_mapping", "method_units", "method_exclusions", "method_history", "history_aggregation_note", "method_snapshot", "method_network"))
    body += '<h2>' + tr.html('provider_methodology') + '</h2>'
    for key, provider in PROVIDERS.items():
        body += '<h3>' + escape(provider['name']) + '</h3><p>' + _badge(_tier_key(provider['tier']),tr) + ' ' + tr.html(provider['note']) + ' <a href="' + escape(provider['source'],quote=True) + '">' + tr.html('source') + '</a></p>'
    from .providers.mappings import CURATED
    mapping_links = {v.get('spec_source',v['source']) for group in ('linode','scaleway','verda') for v in CURATED[group].values()}
    links = sorted(mapping_links | {spec.source_url for spec in AZURE_SKUS.values()} | set(REGION_SOURCES))
    return (body + '<h2>' + tr.html("spec_sources") + '</h2><ul>' + ''.join(
        '<li><a href="' + escape(link, quote=True) + '">' + escape(link.rsplit('/', 1)[-1]) + '</a></li>' for link in links)
        + '</ul><p>' + tr.html("affiliates") + '</p>')


def _page_file(path):
    if path.endswith('/'):
        return path.lstrip('/') + 'index.html'
    return path.lstrip('/') + ('' if path.endswith('.html') else '.html')


def build_site(store: PriceStore, out: str | Path = DEFAULT_OUT, *, today: str | None = None,
               built_at: str | None = None, site_base_url: str | None = None) -> dict:
    """One language-neutral snapshot, then localized HTML; no HTTP or sample data."""
    today = today or datetime.now(timezone.utc).date().isoformat()
    date.fromisoformat(today)
    built = built_at or utc_now()
    origin = site_origin(site_base_url)
    catalogs = load_catalogs()  # Validate every catalog before touching output.
    if origin == DEFAULT_SITE_BASE_URL:
        LOG.warning("SITE_BASE_URL uses placeholder https://example.pages.dev; configure the public origin before launch")
    output = Path(out)
    output.mkdir(parents=True, exist_ok=True)
    all_today = store.current_prices(today, built)
    run = store.latest_run()
    complete_snapshot = True
    offers = [o for o in all_today if parse_utc(o.collected_at)<=parse_utc(built)]
    provider_coverage = coverage_data(store, today)
    monthly = monthly_quotes(store.connection, built)
    observed_rows=[r for r in store.observation_rows() if parse_utc(r['collected_at'])<=parse_utc(built)]
    models = sorted({r['gpu_model'] for r in observed_rows})
    slugs = model_slugs(models)
    exportable_models = {r['gpu_model'] for r in observed_rows if r['tier']=='A' and r['export_allowed']}
    exports, model_data, pages, sitemap_entries = {}, {}, {}, {}
    for model in models:
        current = [offer for offer in offers if offer.gpu_model == model]
        updated = max((o.collected_at for o in current), default=max(r['collected_at'] for r in observed_rows if r['gpu_model']==model))
        histories = {kind: [p for p in store.daily_series(model, price_type=kind) if p['day']<=today] for kind in ("on_demand", "spot")}
        if complete_snapshot:
            for kind, series in histories.items():
                histories[kind] = [point for point in series if point["day"] != today]
                values = [o.price_per_gpu_hour for o in current if o.price_type == kind and eligible(o)]
                if values:
                    histories[kind].append({"day": today, "currency": "USD", "price_type": kind,
                                            "min": min(values), "median": median(values), "max": max(values), "count": len(values)})
                    histories[kind].sort(key=lambda point: point["day"])
        data = {"gpu_model": model, "currency": next(iter({o.currency for o in current})) if len({o.currency for o in current})==1 else "USD" if not current else "MULTI", "observation_day": today,
                "last_updated": updated, "generated_at": built, "source": sorted({PROVIDERS[o.provider]["source"] for o in current if o.provider in PROVIDERS}) or [SOURCE_DOC], "latest_collection": run,
                "offers": [offer.to_dict() for offer in current], "statistics": model_statistics(current, at=built), "history": histories}
        model_data[model] = data
        if model in exportable_models:
            exports['data/' + slugs[model] + '.json'] = {**data, 'offers': [o.to_dict() for o in current if o.tier == 'A' and o.export_allowed], 'statistics': model_statistics([o for o in current if o.tier == 'A' and o.export_allowed], at=built), 'history': {kind: store.daily_series(model, price_type=kind, export_only=True) for kind in ('on_demand','spot')}}
            for kind, series in exports['data/' + slugs[model] + '.json']['history'].items():
                series[:] = [point for point in series if point['day'] < today]
                values = [o.price_per_gpu_hour for o in current if o.price_type == kind and eligible(o) and o.tier == 'A' and o.export_allowed]
                if values:
                    series.append({'day': today, 'currency': 'USD', 'price_type': kind, 'min': min(values), 'median': median(values), 'max': max(values), 'count': len(values)})
            artifact = exports['data/' + slugs[model] + '.json']
            history_rows = [o for o in store.latest_prices(gpu_model=model) if o.tier=='A' and o.export_allowed and parse_utc(o.collected_at)<=parse_utc(built)]
            artifact['last_updated'] = max((o.collected_at for o in history_rows),default=None)
            artifact['source'] = sorted({PROVIDERS[o.provider]['source'] for o in history_rows if o.provider in PROVIDERS}) or [SOURCE_DOC]
            permitted = [o for o in current if o.tier=='A' and o.export_allowed]
            artifact['currency'] = next(iter({o.currency for o in permitted})) if len({o.currency for o in permitted})==1 else 'USD' if not permitted else 'MULTI'
            artifact['latest_collection'] = max((r for r in store.provider_runs().values() if r['provider'] in {o.provider for o in history_rows}),key=lambda r:r['id'],default=None)
    last_updated = max((r['collected_at'] for r in observed_rows),default=None)
    tracked = sorted({r['provider'] for r in observed_rows})
    site_noindex = sum(d['statistics']['offers'] for d in model_data.values()) < 3
    counts = {}
    for lang in LANGUAGES:
        tr = catalogs[lang.code]
        coverage = _coverage(run, tr)
        body, title, description = _home(offers, models, slugs, today, coverage + _provider_coverage(provider_coverage, tr), tr, model_data, monthly)
        website = {"@context": "https://schema.org", "@type": "WebSite", "name": tr.text("site_name"),
                   "url": origin + lang.path(), "description": description, "inLanguage": lang.code}
        pages[_page_file(lang.path())] = _page(title, body, last_updated, built, origin=origin, route="/", tr=tr,
                                               description=description, structured=website, noindex=site_noindex)
        for route in ('/', '/methodology'):
            if not site_noindex:sitemap_entries[lang.path(route)] = (last_updated or built, route)
        for model in models:
            route, export_path = '/' + slugs[model] + '/', ('/data/' + slugs[model] + '.json') if model in exportable_models else None
            data = model_data[model]
            title, description, body, dataset = _model(model, data, today, coverage + _provider_coverage(provider_coverage,tr), tr, origin, route, export_path, exports.get(export_path.lstrip('/')) if export_path else None)
            body+=search.related(model,data,model_data,slugs,tr)
            noindex = data['statistics']['offers'] < 3
            pages[_page_file(lang.path(route))] = _page(title, body, data["last_updated"], built, origin=origin,
                route=route, tr=tr, description=description, structured=dataset, noindex=noindex,
                social_image='/social/'+slugs[model]+'.png')
            if not noindex:
                sitemap_entries[lang.path(route)] = (data["last_updated"] or built, route)
            for number in range(1,(len(data['offers'])+search.OFFER_PAGE_SIZE-1)//search.OFFER_PAGE_SIZE):
                subroute=route+'offers/'+str(number+1)+'/'
                sub_title=tr.text('offer_page',model=model,number=number+1)
                sub_body='<h1>'+escape(sub_title)+'</h1><p>'+tr.html('search_limits')+'</p>'+_offer_browser(search.model_chunk(data,model,number),tr)+search.pagination(model,len(data['offers']),slugs[model],tr)
                pages[_page_file(lang.path(subroute))]=_page(sub_title,sub_body,data['last_updated'],built,origin=origin,route=subroute,tr=tr,description=tr.text('offer_pages_note'),noindex=True,
                    crumbs=[(tr.text('nav_home'),'/'),(model,route),(sub_title,subroute)])
        for key in tracked:
            route='/providers/'+search.provider_slug(key)+'/'
            title,description,body,count=_provider_page(key,model_data,slugs,today,tr)
            updated=max((r.collected_at for r in offers if r.provider==key),default=last_updated)
            pages[_page_file(lang.path(route))]=_page(title,body,updated,built,origin=origin,route=route,tr=tr,description=description,noindex=count<3)
            if count>=3:sitemap_entries[lang.path(route)]=(updated or built,route)
        for slug in PAIRS:
            route='/compare/'+slug+'/'
            title,description,body,count=_comparison_page(slug,model_data,slugs,today,tr)
            pages[_page_file(lang.path(route))]=_page(title,body,last_updated,built,origin=origin,route=route,tr=tr,description=description,noindex=count<3)
            if count>=3:sitemap_entries[lang.path(route)]=(last_updated or built,route)
        method_title = tr.text("method_title")
        method_description = tr.text("method_description", count=tr.quantity("offer_count", len([o for o in offers if eligible(o)])),
                                     models=tr.quantity("model_count", len({o.gpu_model for o in offers if eligible(o)})),
                                     regions=tr.quantity("region_count", len({o.region for o in offers if eligible(o)})), date=today)
        for route, title, description, body, noindex in (
            ("/methodology", method_title, method_description, _methodology(coverage, tr), site_noindex),
            ("/404.html", tr.text("not_found"), tr.text("not_found_description"),
             '<h1>' + tr.html("not_found") + '</h1><p>' + tr.html("not_found_description") + '</p><ul>' + ''.join(
                 '<li><a href="' + other.path() + '" lang="' + other.code + '">' + escape(other.name) + '</a></li>' for other in LANGUAGES)
             + '</ul><p><a href="' + lang.path() + '">' + tr.html("browse") + '</a></p>', True)):
            path = lang.path(route)
            structured = {"@context": "https://schema.org", "@type": "WebPage", "name": title,
                          "description": description, "url": origin + path, "inLanguage": lang.code}
            pages[_page_file(path)] = _page(title, body, last_updated, built, origin=origin, route=route, tr=tr,
                                            description=description, structured=structured, noindex=noindex)
        counts[lang.code] = sum(1 for filename in pages if filename.startswith(lang.prefix+'/')) if lang.prefix else sum(1 for filename in pages if filename.split('/')[0] not in {l.prefix for l in LANGUAGES if l.prefix})
    namespace, xhtml = "http://www.sitemaps.org/schemas/sitemap/0.9", "http://www.w3.org/1999/xhtml"
    ET.register_namespace("", namespace)
    ET.register_namespace("xhtml", xhtml)
    sitemap = ET.Element("{" + namespace + "}urlset")
    for path, (stamp, route) in sorted(sitemap_entries.items()):
        entry = ET.SubElement(sitemap, "{" + namespace + "}url")
        ET.SubElement(entry, "{" + namespace + "}loc").text = origin + path
        ET.SubElement(entry, "{" + namespace + "}lastmod").text = stamp[:10]
        for code, url in _alternates(origin, route).items():
            ET.SubElement(entry, "{" + xhtml + "}link", {"rel": "alternate", "hreflang": code, "href": url})
    other = {"favicon.svg": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="16" fill="#185bc0"/><path d="M16 44V28h8v16zm12 0V20h8v24zm12 0V12h8v32z" fill="white"/></svg>',
             "og.svg": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630"><rect width="1200" height="630" fill="#10223e"/><path d="M80 470V330h55v140zm85 0V240h55v230zm85 0V150h55v320z" fill="#83b5ff"/><text x="370" y="280" fill="white" font-family="system-ui,sans-serif" font-size="64">GPU Price Index</text><text x="370" y="355" fill="#a8b8cf" font-family="system-ui,sans-serif" font-size="30">Multi-provider / GPU / USD</text></svg>',
             "sitemap.xml": '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(sitemap, encoding="unicode") + '\n',
             "robots.txt": 'User-agent: *\nAllow: /\n\nSitemap: ' + origin + '/sitemap.xml\n'+''.join('Sitemap: '+origin+'/sitemap-'+l.code+'.xml\n' for l in LANGUAGES)}
    # Per-language sitemap files contain exactly their indexable HTML URLs.
    for lang in LANGUAGES:
        localized=ET.Element('{'+namespace+'}urlset')
        prefix=lang.path()
        for entry in sitemap:
            url=entry.find('{'+namespace+'}loc').text
            local_path=url[len(origin):]
            language_match=(local_path.startswith(prefix) if lang.prefix else not any(local_path.startswith('/'+l.prefix+'/') for l in LANGUAGES if l.prefix))
            if language_match:localized.append(entry)
        other['sitemap-'+lang.code+'.xml']='<?xml version="1.0" encoding="UTF-8"?>\n'+ET.tostring(localized,encoding='unicode')+'\n'
    for model,data in model_data.items():
        other['social/'+slugs[model]+'.png']=search.social_png(model,data)
    # Hard build gate: no model landing or continuation can grow past 300 KB.
    for lang in LANGUAGES:
        for slug in slugs.values():
            prefix=lang.path('/'+slug+'/').lstrip('/')
            for filename,content in pages.items():
                if filename.startswith(prefix) and len(content.encode('utf-8'))>300_000:
                    raise ValueError('Model HTML exceeds 300 KB: '+filename)
    # Replace only manifest-owned files within output; preserve unrelated files.
    manifest = output / "gpu-prices-manifest.json"
    previous = json.loads(manifest.read_text(encoding="utf-8")).get("files", []) if manifest.exists() else []
    expected = set(pages) | set(exports) | set(other)
    for filename in previous:
        target = _owned_target(output, filename)
        if target is not None and filename not in expected:
            if target.is_file() and not target.is_symlink():
                target.unlink()
                if target.parent != output and not any(target.parent.iterdir()):
                    target.parent.rmdir()
    for filename, content in pages.items():
        _write(output / filename, content)
    for filename, content in exports.items():
        _write(output / filename, json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    for filename, content in other.items():
        _write(output / filename, content)
    _write(manifest, json.dumps({"generated_at": built, "files": sorted(expected), "model_slugs": slugs, "observation_day": today, "site_base_url": origin}, indent=2) + "\n")
    from .audit import audit_site
    audit = audit_site(store.connection, output)
    return {"audit": audit, "models": models, "offers_today": len(offers), "html_pages": len(pages), "html_pages_by_language": counts,
            "out": str(output), "site_base_url": origin, "placeholder_base_url": origin == DEFAULT_SITE_BASE_URL,
            "model_urls": [origin + '/' + slugs[model] + '/' for model in models],
            "model_urls_by_language": {lang.code: [origin + lang.path('/' + slugs[model] + '/') for model in models] for lang in LANGUAGES},
            "json_urls": [origin + '/data/' + slugs[model] + '.json' for model in models if model in exportable_models],
            "sitemap_urls": [origin + path for path in sorted(sitemap_entries)]}


def _owned_target(output: Path, filename) -> Path | None:
    """Manifest entries may be nested, but never escape site/ or own README.md."""
    if (not isinstance(filename, str) or not filename or '\\' in filename
            or any(part in ("", ".", "..") for part in filename.split('/'))
            or Path(filename).is_absolute() or ':' in filename
            or not filename.endswith((".html", ".json", ".xml", ".txt", ".svg", ".png"))):
        return None
    target = output / filename
    if not target.resolve().is_relative_to(output.resolve()):
        return None
    return target


def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if isinstance(content,bytes):temporary.write_bytes(content)
    else:temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)
