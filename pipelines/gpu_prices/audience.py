"""Static reuse and return-visit features. No network, tracking or dependencies."""
from datetime import date, timedelta
from email.utils import format_datetime
from hashlib import sha256
from base64 import b64encode
from html import escape
import json
from statistics import median
import xml.etree.ElementTree as ET

from .models import POPULAR, popular_rank
from .providers.registry import PROVIDERS
from .regions import eligible
from .schema import Offer, parse_utc

LICENSE = 'https://creativecommons.org/licenses/by/4.0/'
EMBED_CSS = ('html{color-scheme:light dark;font:14px system-ui,sans-serif}'
             'body{margin:0;padding:16px;overflow-wrap:anywhere}h1{font-size:18px;margin:0 0 10px}'
             'p{margin:8px 0}.price{font-size:28px;font-weight:700}a{color:LinkText}'
             'small{display:block;line-height:1.5}')


def embed_headers():
    digest = b64encode(sha256(EMBED_CSS.encode('utf-8')).digest()).decode('ascii')
    # frame-ancestors is an HTTP policy, never a meta directive. All locale
    # variants live below /embed/ so no other route receives these headers.
    return ("/embed/*\n  ! X-Frame-Options\n  X-Robots-Tag: noindex\n"
            "  Referrer-Policy: no-referrer\n  X-Content-Type-Options: nosniff\n"
            "  Content-Security-Policy: default-src 'none'; style-src 'sha256-" + digest
            + "'; base-uri 'none'; form-action 'none'; frame-ancestors *\n")


def embed_path(slug, lang):
    return '/embed/' + (lang.prefix + '/' if lang.prefix else '') + slug + '.html'


def money(value):
    return f'${value:.2f}' if value >= 1 else f'${value:.3f}'


def permitted(row):
    return row['tier'] == 'A' and row['export_allowed'] and eligible(Offer(**{
        k: v for k, v in row.items() if k in Offer.__dataclass_fields__}))


def cheapest(data):
    return min((r for r in data['offers'] if permitted(r)), default=None,
               key=lambda r: (r['price_per_gpu_hour'], r['provider'], r['region'] or '', r['sku'], r['price_type']))


def embed_page(model, data, slug, origin, tr):
    row = cheapest(data)
    body = '<h1>' + escape(model) + '</h1><p>' + tr.html('growth_embed_basis') + '</p>'
    if row:
        body += '<p class="price">' + money(row['price_per_gpu_hour']) + '</p><p>' + tr.html('per_gpu_hour') + '</p>'
        labels = [PROVIDERS[row['provider']]['name'], tr.text(row['price_type']),
                  row['region'] or tr.text('unknown_region'), tr.text('tax_' + row['tax_basis']),
                  tr.text('availability') + ': ' + tr.text(row['availability'])]
        body += '<p>' + escape(' · '.join(labels)) + '</p>'
        if row['provider'] == 'clore':
            body += '<small>' + tr.html('community_marketplace') + ' · ' + tr.html('clore_price_note') + '</small>'
        if row['original_currency'] and row['original_currency'] != row['currency']:
            body += '<small>' + escape(f"{row['original_currency']} {row['original_price']:g} / {row['price_scope']} / hr · ECB {row['fx_date']}") + '</small>'
        body += '<small>' + tr.html('growth_observed', stamp=row['collected_at']) + '</small>'
    else:
        body += '<p>' + tr.html('growth_embed_empty') + '</p>'
    body += '<small>' + tr.html('growth_quote_limits') + '</small><p><a href="' + escape(origin + tr.language.path('/' + slug + '/'), quote=True) + '" target="_blank" rel="nofollow noopener">' + tr.html('growth_via') + '</a></p>'
    result = ('<!DOCTYPE html><html lang="' + tr.language.code + '"><head><meta charset="utf-8"/>'
              '<meta name="viewport" content="width=device-width,initial-scale=1"/>'
              '<meta name="robots" content="noindex"/><title>' + escape(model) + ' · ' + tr.html('site_name')
              + '</title><link rel="alternate" type="application/rss+xml" title="' + tr.html('growth_feed_title')
              + '" href="' + escape(origin + tr.language.path('/feed.xml'), quote=True)
              + '"/><style>' + EMBED_CSS + '</style></head><body>' + body + '</body></html>\n')
    if len(result.encode('utf-8')) >= 15_000:
        raise ValueError('Embed HTML must be smaller than 15 KB')
    return result


def embed_section(slug, origin, tr):
    snippet = ('<iframe src="' + origin + embed_path(slug, tr.language) + '" title="' + tr.html('growth_embed_title')
               + '" width="360" height="520" loading="lazy" referrerpolicy="no-referrer"'
               ' sandbox="allow-popups allow-popups-to-escape-sandbox"></iframe>')
    return ('<section data-growth="embed"><h2>' + tr.html('growth_embed_heading') + '</h2><p>'
            + tr.html('growth_embed_help') + '</p><textarea readonly rows="5" cols="40" aria-label="'
            + tr.html('growth_embed_code') + '">' + escape(snippet) + '</textarea><p><a href="'
            + embed_path(slug, tr.language) + '">' + tr.html('growth_embed_preview') + '</a></p></section>')


def data_index(exports, origin, day, built):
    dates = sorted({stamp[:10] for data in exports.values()
                    for stamp in ([data['last_updated']] + [o['collected_at'] for o in data['offers']]
                                  + [p['day'] for series in data['history'].values() for p in series]) if stamp})
    temporal = (dates[0]+'/'+dates[-1]) if dates else None
    return {'schema_version': 1, 'observation_day': day, 'generated_at': built, 'temporal_coverage': temporal,
            'license': LICENSE, 'license_scope': 'compiled data only; provider rights reserved',
            'attribution': 'GPU Price Index', 'attribution_url': origin + '/data/',
            'exports': [{'gpu_model': data['gpu_model'], 'url': origin + '/' + filename,
                         'last_updated': data['last_updated'], 'generated_at': data['generated_at']}
                        for filename, data in sorted(exports.items())]}


def dataset(index, tr, origin):
    """Describe the compilation; upstream content is not relicensed."""
    return {'@context':'https://schema.org', '@type':'Dataset',
            '@id':origin+'/data/#dataset', 'url':origin+tr.language.path('/data/'),
            'name':tr.text('growth_data_title'),
            'description':tr.text('growth_data_intro')+' '+tr.text('growth_license'),
            'license':LICENSE, 'inLanguage':tr.language.code,
            'creator':{'@type':'Organization','name':'GPU Price Index','url':origin+'/'},
            'temporalCoverage':index['temporal_coverage'],
            'distribution':[{'@type':'DataDownload','encodingFormat':'application/json','contentUrl':item['url']}
                            for item in index['exports']]}


def data_body(index, tr):
    groups = (
        ('growth_fields_top', 'gpu_model currency observation_day last_updated generated_at source latest_collection offers statistics history'),
        ('growth_fields_prices', 'price_per_hour price_per_gpu_hour price_scope billing_unit minimum_billing_seconds price_per_day source_billing_unit price_basis'),
        ('growth_fields_hardware', 'provider sku gpu_model gpu_count gpu_fraction vcpu ram_gb region product zone cpu_raw ram_raw vram_raw gpu_model_raw'),
        ('growth_fields_policy', 'price_type currency tier export_allowed tax_basis is_from_price price_conditions availability availability_observed_at'),
        ('growth_fields_time', 'source_url collected_at effective_start_date effective_from effective_to original_currency original_price fx_rate fx_date fx_source fx_usd_per_eur fx_original_per_eur'),
        ('growth_fields_stats', 'statistics: offers all_offers region_count all_region_count cheapest distributions spot_discount_percent spot_no_discount regions; history: on_demand spot day currency price_type min median max count'))
    body = '<h1>' + tr.html('growth_data_title') + '</h1>'
    for key in ('growth_data_intro', 'growth_cadence', 'growth_license', 'growth_attribution'):
        body += '<p>' + tr.html(key) + '</p>'
    body += '<p><a rel="license noopener" href="' + LICENSE + '">CC BY 4.0</a></p>'
    body += '<p data-growth="creator">'+tr.html('seo_creator')+': <a href="'+escape(index['attribution_url'].removesuffix('data/'),quote=True)+'">GPU Price Index</a></p>'
    body += '<p data-growth="temporal">'+tr.html('seo_temporal')+': '+escape(index['temporal_coverage'] or tr.text('not_observed'))+'</p>'
    body += '<p><a href="/data/latest.json">' + tr.html('growth_data_index') + '</a></p>'
    body += '<p><a data-mirror="huggingface" href="https://huggingface.co/datasets/ComputeTicks/gpu-cloud-prices" rel="noopener">' + tr.html('growth_data_mirror') + '</a></p>'
    body += '<h2>' + tr.html('growth_fields_title') + '</h2>'
    for key, fields in groups:
        body += '<p>' + tr.html(key) + '</p><p><code>' + escape(fields) + '</code></p>'
    body += '<h2>' + tr.html('growth_exports_title') + '</h2><ul>'
    for item in index['exports']:
        body += '<li data-growth="export"><a href="' + escape(item['url'], quote=True) + '">' + escape(item['url']) + '</a> · ' + escape(item['last_updated']) + '</li>'
    return body + '</ul><p>' + tr.html('growth_feed_rules') + '</p><p><a href="' + tr.language.path('/feed.xml') + '">' + tr.html('growth_feed_title') + '</a></p>'


def price_drop(data, row, day):
    """Compare the same price type to seven complete previous calendar days."""
    days = {(date.fromisoformat(day) - timedelta(days=n)).isoformat() for n in range(1, 8)}
    series = data['history'].get(row['price_type'], [])
    baseline = [p['min'] for p in series if p['day'] in days]
    current = [p for p in series if p['day'] == day]
    if len(baseline) != 7 or len(current) != 1 or row['price_per_gpu_hour'] != current[0]['min']:
        return None
    middle = median(baseline)
    return {'median': middle, 'minimum': current[0]['min']} if current[0]['min'] <= middle * .9 else None


def badge(data, row, day, tr):
    drop = price_drop(data, row, day)
    return ('<span class="tag" data-growth="drop" data-model="' + escape(row['gpu_model'], quote=True)
            + '" data-kind="' + row['price_type'] + '">' + tr.html('growth_drop') + '</span>') if drop else ''


def events(export_data, rows, day, built):
    """A-only daily evidence, no synthetic dates; stable ids survive rebuilds."""
    result = []
    start = (date.fromisoformat(day) - timedelta(days=29)).isoformat()
    for model, data in sorted(export_data.items()):
        for kind, series in sorted(data['history'].items()):
            previous = []
            for point in sorted(series, key=lambda p: p['day']):
                stamp = point['day']
                yesterday = (date.fromisoformat(stamp) - timedelta(days=1)).isoformat()
                if start <= stamp <= day and previous and previous[-1]['day'] == yesterday:
                    before = previous[-1]['min']
                    reason = ('drop' if point['min'] <= before * .85 else
                              'low' if popular_rank(model) < len(POPULAR) and point['min'] < min(p['min'] for p in previous) else None)
                    if reason:
                        result.append({'day': stamp, 'reason': reason, 'model': model, 'kind': kind,
                                       'price': point['min'], 'before': before})
                previous.append(point)
    first = {}
    observed = set()
    active = {o['provider'] for d in export_data.values() for o in d['offers'] if permitted(o)}
    for row in rows:
        if (row['day'] > day or parse_utc(row['collected_at']) > parse_utc(built) or not permitted(row)
                or not Offer(**{k: v for k, v in row.items() if k in Offer.__dataclass_fields__}).current_at(row['day'] + 'T23:59:59.999999Z')):
            continue
        # Current-day failures/absent offers must not create new-provider news.
        if row['day'] == day and row['provider'] not in active:
            continue
        observed.add(row['day'])
        first[row['provider']] = min(first.get(row['provider'], row['day']), row['day'])
    for provider, stamp in sorted(first.items()):
        if start <= stamp <= day and (date.fromisoformat(stamp) - timedelta(days=1)).isoformat() in observed:
            result.append({'day': stamp, 'reason': 'provider', 'provider': provider})
    return sorted(result, key=lambda e: (-date.fromisoformat(e['day']).toordinal(), json.dumps(e, sort_keys=True)))[:50]


def feed(items, slugs, origin, day, tr):
    root = ET.Element('rss', {'version': '2.0'})
    channel = ET.SubElement(root, 'channel')
    for key, value in (('title', tr.text('growth_feed_title')), ('link', origin + tr.language.path('/data/')),
                       ('description', tr.text('growth_feed_rules')), ('language', tr.language.code)):
        ET.SubElement(channel, key).text = value
    for event in items:
        node = ET.SubElement(channel, 'item')
        reason = event['reason']
        if reason == 'provider':
            title = tr.text('growth_feed_provider', provider=PROVIDERS[event['provider']]['name'], date=event['day'])
            route = '/providers/' + ('clore-ai' if event['provider'] == 'clore' else event['provider'].replace('_', '-')) + '/'
        else:
            title = tr.text('growth_feed_' + reason, model=event['model'], kind=tr.text(event['kind']),
                            price=money(event['price']), before=money(event['before']), date=event['day'])
            route = '/' + slugs[event['model']] + '/'
        identity = sha256(json.dumps(event, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        ET.SubElement(node, 'title').text = title
        ET.SubElement(node, 'link').text = origin + tr.language.path(route)
        ET.SubElement(node, 'guid', {'isPermaLink': 'false'}).text = origin + '/feed.xml#' + identity
        ET.SubElement(node, 'pubDate').text = format_datetime(parse_utc(event['day'] + 'T00:00:00Z'), usegmt=True)
        ET.SubElement(node, 'description').text = title + ' ' + tr.text('growth_feed_limits')
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding='unicode') + '\n'
