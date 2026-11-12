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
from .schema import parse_utc, utc_now
from .store import PriceStore, ROOT
from .regions import eligible, region_class, REGION_SOURCES
from .links import link_for, PROVIDER_ONLY
from .providers.registry import PROVIDERS

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
<meta name="theme-color" content="#185bc0"/><link rel="icon" href="$origin/favicon.svg" type="image/svg+xml"/><meta property="og:image" content="$origin/og.svg"/>
<meta name="twitter:card" content="summary"/><meta name="twitter:title" content="$title"/><meta name="twitter:description" content="$description"/>
$robots$structured<style>$css</style></head>
<body><a class="skip" href="#main">$skip</a><header><a class="brand" href="$home">$site_name</a><nav aria-label="$navigation"><a href="$home">$nav_home</a><a href="$models_path">$nav_models</a><a href="$method_path">$method_label</a></nav>$switcher</header>
<main id="main">$placeholder$body
<!-- Future affiliate link slot: intentionally empty until reviewed and disclosed. -->
<div class="affiliate-slot" data-slot="future-affiliate"></div></main>
<footer><div class="footer-grid"><p>$updated<br/>$generated</p><p>$disclosure</p></div>
<p>$footer_source</p><p id="contact">$contact_label <span>&#122;&#104;&#97;&#110;&#57;&#57;&#57; [at] &#116;&#117;&#116;&#97;&#109;&#97;&#105;&#108; [dot] &#99;&#111;&#109;</span> &middot; <a href="$method_path">$method_label</a></p></footer></body></html>
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
    reserved = {"data", "index", "methodology", "404", "robots", "sitemap"} | {lang.prefix for lang in LANGUAGES if lang.prefix}
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


def _page(title, body, updated, built, *, origin, route, tr, description, structured=None, noindex=False):
    path = tr.language.path(route)
    # HTML escaping corrupts JSON strings; JSON escapes keep it parseable and
    # prevent model/region text from closing the inert application/ld+json block.
    return PAGE.substitute(origin=origin, nav_home=tr.html("nav_home"), nav_models=tr.html("nav_models"), models_path=tr.language.path()+"#models", disclosure=tr.html("disclosure"), title=escape(title), page_title=tr.html("page_title", title=title, site_name=tr.text("site_name")),
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
                           footer_source=tr.rich("footer_source", source='<a href="' + SOURCE_DOC + '">' + tr.html("source_name") + '</a>'))


def model_statistics(offers) -> dict:
    """Only eligible whole-GPU standard-region offers drive comparisons."""
    offers = [o for o in offers if o.current_at(o.collected_at)]
    selected = [o for o in offers if eligible(o)]
    distributions = {}
    for kind in ("on_demand", "spot"):
        values = [o.price_per_gpu_hour for o in selected if o.price_type == kind]
        distributions[kind] = ({"min": min(values), "median": median(values), "max": max(values), "count": len(values)} if values else None)
    cheapest = {k: v["min"] if v else None for k, v in distributions.items()}
    regions = []
    for region in sorted({o.region for o in offers}, key=lambda r: r or ""):
        for allocation in ("whole", "fractional"):
            local = [o for o in offers if o.region == region and (o.gpu_count >= 1 and float(o.gpu_count).is_integer() and o.gpu_fraction == 1) == (allocation == "whole")]
            if not local:
                continue
            regions.append({"region": region, "classification": region_class(region), "allocation": allocation, "offers": len(local),
                            **{k + "_min": min((o.price_per_gpu_hour for o in local if o.price_type == k and o.currency == "USD" and o.tax_basis != "included" and not o.is_from_price and o.price_scope != "gpu_component"), default=None) for k in cheapest}})
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
    formatted = _money(value) if fmt == "money" else f"{value:.2f}%" if fmt == "percent" else f"{value:g}" if fmt == "number" else str(value)
    return '<' + tag + ' data-stat="' + escape(path, quote=True) + '" data-format="' + fmt + '">' + escape(formatted) + '</' + tag + '>'


def _value(path, value, tr, fmt="money"):
    return _stat(path, value, fmt) if value is not None else tr.html("not_observed")


def _outbound(provider, sku, region, kind, model, tr):
    link = link_for(provider, sku, region, kind, model=model)
    rel = "sponsored nofollow noopener" if link["is_affiliate"] else "nofollow noopener"
    return '<a class="button" href="' + escape(link["url"], quote=True) + '" target="_blank" rel="' + rel + '">' + tr.html(link["label"]) + '</a>'


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
        region_rows.append('<tr>' + _cell(region["region"] or tr.text("unknown_region")) + '<td>' + _badge(region["classification"], tr) + _badge("whole_gpu" if region["allocation"] == "whole" else "fractional", tr) + '</td>'
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
        status = ('link_only' if provider['tier']=='D' else 'disabled') if not provider['enabled'] else ('stale_provider' if run and (run['status']=='failed' or run['started_at'][:10] != today) else run['status'] if run else 'not_collected')
        result.append({'provider':key, 'name':provider['name'], 'tier':provider['tier'], 'status':status,
                       'model_count':len({o.gpu_model for o in rows if o.provider==key}), 'last_success':success})
    return result


def _provider_coverage(data, tr):
    rows = []
    for i, entry in enumerate(data):
        path = '/coverage/'+str(i)
        rows.append('<tr>'+_cell(entry['name'])+'<td>'+_badge('official_api' if entry['tier']=='A' else 'link_only',tr)+'</td><td>'+_stat(path+'/model_count',entry['model_count'],'text')+'</td><td>'+(_stat(path+'/last_success',entry['last_success'],'text') if entry['last_success'] else tr.html('not_collected'))+'</td><td>'+tr.html(entry['status'])+'</td></tr>')
    return '<h2>'+tr.html('provider_coverage')+'</h2>'+_table([tr.text(k) for k in ('provider','tier_label','models_tracked','last_success','health_status')],rows,tr.text('provider_coverage'))


def _filters(rows, tr):
    result = '<div class="filters">'
    for field, values in (('provider', sorted({r['provider'] for r in rows})), ('price_type', sorted({r['price_type'] for r in rows})), ('region_class', sorted({region_class(r['region']) for r in rows}))):
        result += '<label>'+tr.html(field)+' <select data-filter="'+field.replace('_','-')+'"><option value="">'+tr.html('filter_all')+'</option>'
        for value in values:
            label = PROVIDERS.get(value,{}).get('name',value) if field=='provider' else tr.text(value)
            result += '<option value="'+escape(value,quote=True)+'">'+escape(label)+'</option>'
        result += '</select></label> '
    return result + '</div>'


FILTER_JS = '''<script type="module">
const filters=[...document.querySelectorAll('[data-filter]')];
for(const filter of filters) filter.addEventListener('change',()=>{
 for(const row of document.querySelectorAll('tr[data-provider]'))
  row.hidden=filters.some(f=>f.value && row.getAttribute('data-'+f.dataset.filter)!==f.value);
});
</script>'''


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
            parts.append(f'<circle cx="{x(d):.2f}" cy="{y(p[key]):.2f}" r="3" fill="{color}"><title>'+_stat(base+'/'+str(i)+'/day',p['day'],'text')+': '+tr.html(key if key=='median' else 'minimum' if key=='min' else 'maximum')+' '+_stat(base+'/'+str(i)+'/'+key,p[key]) + (' (n='+_stat(base+'/'+str(i)+'/count',p['count'],'text')+')' if key=='median' else '')+'</title></circle>')
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


SORT_JS = """<script type="module">
for(const table of document.querySelectorAll('[data-sortable]')){
 for(const [column,th] of [...table.tHead.rows[0].cells].entries()){
  const button=document.createElement('button');button.textContent=th.textContent+' ↕';th.textContent='';th.append(button);
  button.addEventListener('click',()=>{
   const descending=th.getAttribute('aria-sort')==='ascending';
   for(const head of table.tHead.rows[0].cells)head.removeAttribute('aria-sort');
   th.setAttribute('aria-sort',descending?'descending':'ascending');
   const value=row=>{const text=row.cells[column].textContent.trim();const num=Number(text.replace(/[$%,]/g,''));return text!==''&&Number.isFinite(num)?num:text;};
   const rows=[...table.tBodies[0].rows];rows.sort((a,b)=>{const x=value(a),y=value(b);return (typeof x==='number'&&typeof y==='number'?x-y:String(x).localeCompare(String(y)))*(descending?-1:1);});
   table.tBodies[0].append(...rows);
  });
 }
}
</script>"""


def _home(offers, models, slugs, today, coverage, tr):
    selected = [o for o in offers if eligible(o)]
    active = sorted({o.gpu_model for o in selected})
    body = '<section class="hero"><p class="eyebrow">' + tr.html("eyebrow") + '</p><h1>' + tr.html("home_heading") + '</h1><p>' + tr.html("home_intro") + '</p><p class="muted">' + tr.rich("updated", date=_timestamp(max((o.collected_at for o in offers), default=None), tr, '/current_updated')) + '</p></section>'
    body += '<div class="grid kpis">'
    for key, value, label in (("models", len(active), "models_tracked"), ("offers", len(selected), "eligible_offers"), ("regions", len({o.region for o in selected}), "eligible_regions")):
        body += '<article class="card"><p>' + tr.html(label) + '</p><strong class="metric">' + _stat('/summary/' + key, value, 'text') + '</strong></article>'
    for model in ("H100 SXM 80GB", "H200 141GB", "A100 80GB"):
        stats = model_statistics([o for o in offers if o.gpu_model == model])
        body += '<article class="card demand"><p>' + escape(model) + ' / ' + tr.html("on_demand_from") + '</p><strong class="metric">' + _value(_pointer('models', model, 'statistics', 'cheapest', 'on_demand'), stats['cheapest']['on_demand'], tr) + '</strong><p class="unit">' + tr.html('gpu_hour') + '</p></article>'
    body += '</div>' + coverage + '<h2 id="models">' + tr.html('models_heading') + '</h2><details class="basis"><summary>' + tr.html('definitions_heading') + '</summary><p>' + tr.html('basis') + ' ' + tr.html('maximum_def') + '</p></details>'
    rows = []
    for model in active:
        stats = model_statistics([o for o in offers if o.gpu_model == model]);base=_pointer('models', model, 'statistics')
        rows.append('<tr><td><a href="' + tr.language.path('/' + slugs[model] + '/') + '">' + escape(model) + '</a></td>'
            + ''.join('<td class="price number">' + _value(base+'/cheapest/'+k,stats['cheapest'][k],tr) + '</td>' for k in ('on_demand','spot'))
            + '<td>' + _discount(stats,base,tr) + '</td><td class="number">' + _stat(base+'/region_count',stats['region_count'],'text') + '</td><td class="number">' + _stat(base+'/offers',stats['offers'],'text') + '</td></tr>')
    if rows:
        body += _table([tr.text(k) for k in ('gpu_model','cheapest_on_demand','cheapest_spot','spot_discount','eligible_regions','offers')],rows,tr.text('home_caption')).replace('<table>', '<table data-sortable="true">') + SORT_JS
    else:
        body += '<p class="notice">' + tr.html('empty_today') + '</p>'
    if models:
        body += '<ul>' + ''.join('<li><a href="' + tr.language.path('/'+slugs[m]+'/') + '">' + escape(m) + '</a></li>' for m in models) + '</ul>'
    body += _provider_cards(tr)
    numbers = dict(count=tr.quantity('offer_count',len(selected)),models=tr.quantity('model_count',len(active)),regions=tr.quantity('region_count',len({o.region for o in selected})),date=today)
    return body,tr.text('home_title',**numbers),tr.text('home_description',**numbers)


def _faq(model, stats, today, tr):
    demand, spot = stats['cheapest']['on_demand'], stats['cheapest']['spot']
    comparison = tr.text('no_discount') if stats['spot_no_discount'] else (tr.text('discount_below',percent=f"{stats['spot_discount_percent']:.2f}") if stats['spot_discount_percent'] is not None else tr.text('not_observed'))
    answers = [tr.text('faq_cheapest_a',demand=_money(demand) if demand is not None else tr.text('not_observed'),spot=_money(spot) if spot is not None else tr.text('not_observed'),regions=stats['region_count'],offers=stats['offers'],date=today),
               tr.text('faq_spot_a',comparison=comparison),tr.text('faq_units_a')]
    questions = [tr.text('faq_cheapest_q',model=model),tr.text('faq_spot_q'),tr.text('faq_units_q')]
    return list(zip(questions,answers))


def _model(model, data, today, coverage, tr, origin, route, export_path):
    current,stats,updated=data['offers'],data['statistics'],data['last_updated']
    base=_pointer('models',model)
    if current:
        title=tr.text('model_title',model=model,price=_money(stats['cheapest']['on_demand']) if stats['cheapest']['on_demand'] is not None else tr.text('not_observed'),
            spot_price=_money(stats['cheapest']['spot']) if stats['cheapest']['spot'] is not None else tr.text('not_observed'),count=tr.quantity('offer_count',stats['offers']),regions=tr.quantity('azure_region_count',stats['region_count']),date=updated[:10])
    else:
        title=tr.text('model_empty_title',model=model,count=tr.quantity('offer_count',0),regions=tr.quantity('azure_region_count',0),date=today)
        if updated:title=tr.text('last_observed',title=title,date=updated[:10])
    description=tr.text('model_description',title=title)
    body='<section class="hero"><p class="eyebrow">' + tr.html('eyebrow') + '</p><h1>' + escape(model) + '</h1><p>' + tr.html('model_intro',date=today) + '</p>'
    hero=next((o for o in current if o['gpu_count']>=1 and region_class(o['region'])=='standard' and o['price_type']=='on_demand' and o['price_per_gpu_hour']==stats['cheapest']['on_demand']),None)
    if hero:body+=_outbound(hero['provider'],hero['sku'],hero['region'],hero['price_type'],model,tr)
    elif current:body+=_outbound(current[0]['provider'],current[0]['sku'],current[0]['region'],current[0]['price_type'],model,tr)
    if current:body+=_price_cards(model,stats,tr)
    body+='</section>' + coverage
    if not current:body+='<p class="notice">' + tr.html('stale') + '</p>'
    else:
        body+=_model_content(model,stats,today,tr)
        body+=_filters(current,tr)+'<h2>' + tr.html('sku_heading') + '</h2><p>' + tr.html('sku_intro') + '</p>'
        for fractional in (False,True):
            rows=[]
            for i,o in enumerate(current):
                if (o['gpu_count']<1 or not float(o['gpu_count']).is_integer() or o['gpu_fraction']<1)!=fractional:continue
                path=base+'/offers/'+str(i)
                share=__import__('fractions').Fraction(min(o['gpu_count'],o['gpu_fraction'])).limit_denominator(12)
                badges=_badge(region_class(o['region']),tr)+_badge('fractional' if fractional else 'whole_gpu',tr)+_badge('official_api' if o['tier']=='A' else 'public_page',tr)+_badge('tax_'+o['tax_basis'],tr)+'<br/>'+tr.html('availability')+': '+tr.html(o['availability'])+'<br/>'+tr.html('price_scope')+': '+tr.html('scope_'+o['price_scope'])
                if o['is_from_price']:badges+=_badge('from_price',tr)
                if fractional:badges+='<p class="unit">'+tr.html('fraction_note',share=str(share))+'</p>'
                rows.append('<tr data-provider="'+escape(o['provider'],quote=True)+'" data-price-type="'+o['price_type']+'" data-region-class="'+region_class(o['region'])+'">' + _cell(tr.text(o['price_type'])) + '<td class="price number">' + escape(o['currency'])+' '+_stat(path+'/price_per_gpu_hour',o['price_per_gpu_hour'], 'money' if o['currency']=='USD' else 'number') + '</td><td class="number">' + escape(o['currency'])+' '+_stat(path+'/price_per_hour',o['price_per_hour'], 'money' if o['currency']=='USD' else 'number')+_original(o,path,tr) + '</td>'
                    + ''.join('<td class="number">'+(_stat(path+'/'+key,o[key],'number') if o[key] is not None else tr.html('unknown_value'))+'</td>' for key in ('gpu_count','vcpu','ram_gb'))
                    + _cell(PROVIDERS.get(o['provider'],{}).get('name',o['provider']))+_cell(o['region'] or tr.text('unknown_region'))+_cell(o['sku'])+'<td>'+badges+'</td><td>'+_outbound(o['provider'],o['sku'],o['region'],o['price_type'],model,tr)+'<br/><a href="'+escape(o['source_url'],quote=True)+'">'+tr.html('api_record')+'</a></td><td>'+_timestamp(o['collected_at'],tr,path+'/collected_at')+'</td></tr>')
            if rows:
                if fractional:body+='<h3>'+tr.html('fraction_heading')+'</h3>'
                body+=_table([tr.text(k) for k in ('price_type','sku_gpu_hour','sku_vm_hour','gpu_share','vcpu','ram','provider','region','sku','allocation','source','observed_utc')],rows,tr.text('fraction_heading' if fractional else 'offers_caption'))
    body+=FILTER_JS+'<h2>'+tr.html('history_heading')+'</h2><p>'+tr.html('history_intro')+'</p>'
    for kind,series in data['history'].items():
        if series:body+='<h3>'+tr.html(kind)+'</h3>'+_chart(series,kind,tr,base+'/history/'+kind)
    if export_path:
        body+='<p><a href="'+export_path+'">'+tr.html('download')+'</a></p>'
    dataset={"@context":"https://schema.org","@type":"Dataset","@id":origin+tr.language.path(route)+'#dataset',"name":tr.text('dataset_name',model=model),"description":description,"url":origin+tr.language.path(route),"isBasedOn":data["source"],"inLanguage":tr.language.code,
        "spatialCoverage":[{"@type":"Place","name":r} for r in sorted({r['region'] for r in stats['regions']},key=lambda r:r or '')],"variableMeasured":{"@type":"PropertyValue","name":"price_per_gpu_hour","unitText":"USD/GPU-hour"},}
    if export_path:dataset['distribution']={'@type':'DataDownload','encodingFormat':'application/json','contentUrl':origin+export_path}
    if updated:dataset['dateModified']=updated
    if current:dataset['temporalCoverage']=today
    structured=[dataset]
    if current:
        faqs=_faq(model,stats,today,tr)
        body+='<section class="faq"><h2>'+tr.html('faq_heading',model=model)+'</h2>'+''.join('<details><summary>'+escape(q)+'</summary><p data-faq="'+str(i)+'">'+escape(a)+'</p></details>' for i,(q,a) in enumerate(faqs))+'</section>'
        structured.append({"@context":"https://schema.org","@type":"FAQPage","inLanguage":tr.language.code,"url":origin+tr.language.path(route),"mainEntity":[{"@type":"Question","name":q,"acceptedAnswer":{"@type":"Answer","text":a}} for q,a in faqs]})
    return title,description,body,structured


def _methodology(coverage, tr):
    source = '<a href="' + SOURCE_DOC + '">' + tr.html("source_name") + '</a>'
    body = '<h1>' + tr.html("methodology") + '</h1>' + coverage + '<p>' + tr.rich("method_api", source=source) + '</p>'
    body += ''.join('<p>' + tr.html(key) + '</p>' for key in (
        "basis", "maximum_def", "method_regions", "method_rounding", "method_cadence", "method_audit", "method_filters", "method_mapping", "method_units", "method_exclusions", "method_history", "method_snapshot", "method_network"))
    body += '<h2>' + tr.html('provider_methodology') + '</h2>'
    for key, provider in PROVIDERS.items():
        body += '<h3>' + escape(provider['name']) + '</h3><p>' + _badge('official_api' if provider['tier']=='A' else 'link_only',tr) + ' ' + tr.html(provider['note']) + ' <a href="' + escape(provider['source'],quote=True) + '">' + tr.html('source') + '</a></p>'
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
    offers = all_today
    provider_coverage = coverage_data(store, today)
    models = store.models()
    slugs = model_slugs(models)
    exportable_models = {r[0] for r in store.connection.execute("SELECT DISTINCT gpu_model FROM offers WHERE tier='A' AND export_allowed=1")}
    exports, model_data, pages, sitemap_entries = {}, {}, {}, {}
    for model in models:
        current = [offer for offer in offers if offer.gpu_model == model]
        updated = max((o.collected_at for o in current), default=store.last_updated(model))
        histories = {kind: store.daily_series(model, price_type=kind) for kind in ("on_demand", "spot")}
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
                "offers": [offer.to_dict() for offer in current], "statistics": model_statistics(current), "history": histories}
        model_data[model] = data
        if model in exportable_models:
            exports['data/' + slugs[model] + '.json'] = {**data, 'offers': [o.to_dict() for o in current if o.tier == 'A' and o.export_allowed], 'statistics': model_statistics([o for o in current if o.tier == 'A' and o.export_allowed]), 'history': {kind: store.daily_series(model, price_type=kind, export_only=True) for kind in ('on_demand','spot')}}
            for kind, series in exports['data/' + slugs[model] + '.json']['history'].items():
                series[:] = [point for point in series if point['day'] != today]
                values = [o.price_per_gpu_hour for o in current if o.price_type == kind and eligible(o) and o.tier == 'A' and o.export_allowed]
                if values:
                    series.append({'day': today, 'currency': 'USD', 'price_type': kind, 'min': min(values), 'median': median(values), 'max': max(values), 'count': len(values)})
            artifact = exports['data/' + slugs[model] + '.json']
            history_rows = [o for o in store.latest_prices(gpu_model=model) if o.tier=='A' and o.export_allowed]
            artifact['last_updated'] = max((o.collected_at for o in history_rows),default=None)
            artifact['source'] = sorted({PROVIDERS[o.provider]['source'] for o in history_rows if o.provider in PROVIDERS}) or [SOURCE_DOC]
            permitted = [o for o in current if o.tier=='A' and o.export_allowed]
            artifact['currency'] = next(iter({o.currency for o in permitted})) if len({o.currency for o in permitted})==1 else 'USD' if not permitted else 'MULTI'
            artifact['latest_collection'] = max((r for r in store.provider_runs().values() if r['provider'] in {o.provider for o in history_rows}),key=lambda r:r['id'],default=None)
    last_updated = store.last_updated()
    counts = {}
    for lang in LANGUAGES:
        tr = catalogs[lang.code]
        coverage = _coverage(run, tr)
        body, title, description = _home(offers, models, slugs, today, coverage, tr)
        body += _provider_coverage(provider_coverage, tr)
        website = {"@context": "https://schema.org", "@type": "WebSite", "name": tr.text("site_name"),
                   "url": origin + lang.path(), "description": description, "inLanguage": lang.code}
        pages[_page_file(lang.path())] = _page(title, body, last_updated, built, origin=origin, route="/", tr=tr,
                                               description=description, structured=website)
        for route in ('/', '/methodology'):
            sitemap_entries[lang.path(route)] = (last_updated or built, route)
        for model in models:
            route, export_path = '/' + slugs[model] + '/', ('/data/' + slugs[model] + '.json') if model in exportable_models else None
            data = model_data[model]
            title, description, body, dataset = _model(model, data, today, coverage + _provider_coverage(provider_coverage,tr), tr, origin, route, export_path)
            noindex = len(data["offers"]) < 3
            pages[_page_file(lang.path(route))] = _page(title, body, data["last_updated"], built, origin=origin,
                route=route, tr=tr, description=description, structured=dataset, noindex=noindex)
            if not noindex:
                sitemap_entries[lang.path(route)] = (data["last_updated"] or built, route)
        method_title = tr.text("method_title")
        method_description = tr.text("method_description", count=tr.quantity("offer_count", len([o for o in offers if eligible(o)])),
                                     models=tr.quantity("model_count", len({o.gpu_model for o in offers if eligible(o)})),
                                     regions=tr.quantity("region_count", len({o.region for o in offers if eligible(o)})), date=today)
        for route, title, description, body, noindex in (
            ("/methodology", method_title, method_description, _methodology(coverage, tr), False),
            ("/404.html", tr.text("not_found"), tr.text("not_found_description"),
             '<h1>' + tr.html("not_found") + '</h1><p>' + tr.html("not_found_description") + '</p><ul>' + ''.join(
                 '<li><a href="' + other.path() + '" lang="' + other.code + '">' + escape(other.name) + '</a></li>' for other in LANGUAGES)
             + '</ul><p><a href="' + lang.path() + '">' + tr.html("browse") + '</a></p>', True)):
            path = lang.path(route)
            structured = {"@context": "https://schema.org", "@type": "WebPage", "name": title,
                          "description": description, "url": origin + path, "inLanguage": lang.code}
            pages[_page_file(path)] = _page(title, body, last_updated, built, origin=origin, route=route, tr=tr,
                                            description=description, structured=structured, noindex=noindex)
        counts[lang.code] = len(models) + 3  # Home, methodology, localized 404, models.
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
             "robots.txt": 'User-agent: *\nAllow: /\n\nSitemap: ' + origin + '/sitemap.xml\n'}
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
            or not filename.endswith((".html", ".json", ".xml", ".txt", ".svg"))):
        return None
    target = output / filename
    if not target.resolve().is_relative_to(output.resolve()):
        return None
    return target


def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)
