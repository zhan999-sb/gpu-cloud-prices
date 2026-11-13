"""Weekly editorial snapshots from retained daily groups, without network IO."""
from datetime import date, timedelta
from html import escape

from .schema import Offer, parse_utc
from .regions import eligible
from .models import POPULAR, popular_rank
from .providers.registry import PROVIDERS
from .search import provider_slug
from .guide_specs import GUIDES

# History retains group minima, not all SKUs. Never pretend to track a machine.
GROUP = ('provider', 'gpu_model', 'gpu_count', 'gpu_fraction', 'price_type',
         'region', 'currency', 'price_scope', 'tax_basis', 'billing_unit',
         'minimum_billing_seconds', 'original_currency', 'price_conditions',
         'source_billing_unit', 'price_basis')


def week(day):
    year, number, _ = date.fromisoformat(day).isocalendar()
    return f'{year}-W{number:02d}'


def snapshot(rows, current, day, built):
    retained = [r for r in rows if r['day'] <= day and parse_utc(r['collected_at']) <= parse_utc(built)]
    daily = {}
    for row in retained:
        offer = Offer(**{k: row[k] for k in Offer.__dataclass_fields__})
        if eligible(offer) and offer.current_at(row['day']+'T23:59:59.999999Z'):
            daily.setdefault(row['day'], []).append(row)
    daily[day] = [r for r in current if eligible(Offer(**r))]
    endpoints = {week(r['day']): r['day'] for r in sorted(retained, key=lambda r: r['day'])}
    endpoints[week(day)] = day
    result = {}
    for key, end in sorted(endpoints.items(), reverse=True):
        before = (date.fromisoformat(end)-timedelta(days=7)).isoformat()
        now, old = daily.get(end, []), daily.get(before, [])
        def groups(population):
            selected = {}
            for r in population:
                identity = tuple(r.get(k) or None if k == 'region' else r.get(k) for k in GROUP)
                if identity not in selected or (r['price_per_gpu_hour'], r['sku']) < (selected[identity]['price_per_gpu_hour'], selected[identity]['sku']):
                    selected[identity] = r
            return selected
        a, b = groups(now), groups(old)
        pairs = []
        for identity in a.keys() & b.keys():
            r, previous = a[identity], b[identity]
            if popular_rank(r['gpu_model']) >= len(POPULAR):
                continue
            pairs.append(dict(now=r, before=previous,
                              change=(r['price_per_gpu_hour']/previous['price_per_gpu_hour']-1)*100))
        def order(r):
            return (popular_rank(r['gpu_model']), r['gpu_model'], r['price_type'],
                    r['price_per_gpu_hour'], r['provider'], r['region'] or '', r['sku'])
        selected = {}
        for p in sorted(pairs, key=lambda p: order(p['now'])):
            selected.setdefault((p['now']['gpu_model'], p['now']['price_type']), p)
        comparisons = list(selected.values())
        minima = {}
        for r in sorted(now, key=order):
            if popular_rank(r['gpu_model']) < len(POPULAR):
                minima.setdefault((r['gpu_model'], r['price_type']), r)
        changes = {}
        # No baseline is an unknown state, never an initial-import announcement.
        for field, label in (('gpu_model', 'models'), ('provider', 'providers')):
            x, y = {r[field] for r in now}, {r[field] for r in old}
            changes['new_'+label] = sorted(x-y) if now and old else []
            changes['missing_'+label] = sorted(y-x) if now and old else []
        observed = [r['collected_at'] for r in retained if week(r['day']) == key and r['day'] <= end]
        result[key] = dict(week=key, day=end, before_day=before,
            first_day=min((r['day'] for r in retained), default=None),
            offers=len(now), providers=len({r['provider'] for r in now}),
            before_offers=len(old), before_providers=len({r['provider'] for r in old}),
            # Historical counts describe retained groups, not the original full catalog.
            current=end == day, baseline=bool(old), minima=list(minima.values()), comparisons=comparisons,
            moves=sorted((p for p in comparisons if p['change'] != 0),
                         key=lambda p: (-abs(p['change']), order(p['now'])))[:10],
            indexable=len(comparisons) >= 3, **changes,
            # This is a visible observation date, not an article publication date.
            published=min(observed, default=None), updated=max(observed, default=None))
    return result


def title(key, tr):
    return tr.text('report_title', week=key)


def qualifiers(row, tr):
    parts=[tr.text('gpu_count')+': '+format(row['gpu_count'],'g'),tr.text('tax_'+row['tax_basis']),
           tr.text({'A':'official_api','B':'tier_b','C':'tier_c'}[row['tier']]),tr.text('scope_'+row['price_scope']),
           tr.text('billing_'+row['billing_unit'].replace('-','_')),
           tr.text('availability')+': '+tr.text(row['availability'])]
    if row['minimum_billing_seconds'] is not None:
        parts.append(tr.text('minimum_billing')+': '+str(row['minimum_billing_seconds'])+' '+tr.text('seo_seconds'))
    if row['provider']=='clore':parts.append(tr.text('clore_price_note'))
    if row.get('price_conditions'):parts.append(row['price_conditions'])
    if row.get('original_currency') and row['original_currency']!='USD':
        parts.append(tr.text('original_hourly')+': '+row['original_currency']+' '+format(row['original_price'],'g'))
        parts.append(tr.text('fx_rate_label')+': '+format(row['fx_rate'],'g')+'; ECB '+row['fx_date'])
    parts.append(tr.text('report_asof')+': '+row['collected_at'])
    return ' · '.join(parts)


def render(key, data, slugs, tr, *, stat, table):
    """All factual text has a marker and is independently reconstructed by audit."""
    def prose(name):
        return '<p data-search="report:'+name+'">'+tr.html('report_'+name)+'</p>'
    def link(route, text):
        return '<a href="'+tr.language.path(route)+'">'+escape(text)+'</a>'
    def model(name):
        return link('/'+slugs[name]+'/', name)
    def provider(name):
        return link('/providers/'+provider_slug(name)+'/', PROVIDERS[name]['name'])
    def quote(row):
        return [model(row['gpu_model']), tr.html(row['price_type']), provider(row['provider']),
                escape(row['region'] or tr.text('unknown_region'))]
    def conditions(section,i,now,before=None):
        text=tr.text('report_now')+': '+qualifiers(now,tr)
        if before:text+='; '+tr.text('report_previous')+': '+qualifiers(before,tr)
        sources=[now]+([before] if before else [])
        return '<small data-search="report:quote:'+section+':'+str(i)+'">'+escape(text)+''.join(' · <a href="'+escape(r['source_url'],quote=True)+'">'+tr.html('source')+'</a>' for r in sources)+'</small>'
    def rows(headers, values):
        return table([tr.text(k) for k in headers], ['<tr>'+''.join('<td>'+c+'</td>' for c in r)+'</tr>' for r in values], tr.text('report_units'))
    if key is None:
        body='<section data-report="index"><h1>'+tr.html('report_index_title')+'</h1>'+prose('index_intro')
        body+='<ul>'+''.join('<li>'+link('/reports/'+k+'/', title(k,tr))+' <span data-search="report:status:'+k+'">'+tr.html('report_ready' if d['indexable'] else 'report_thin')+'</span></li>' for k,d in data.items())+'</ul></section>'
        return tr.text('report_index_title'), tr.text('report_index_intro'), body, any(d['indexable'] for d in data.values())
    d=data[key]; stem='/reports/'+key+'/'
    def value(field, v, fmt='number'):
        return stat(stem+field, v, fmt)
    def qvalue(section, i, field, v, fmt='money'):
        return value(section+'/'+str(i)+'/'+field, v, fmt)
    body='<article data-report="'+key+'"><h1>'+escape(title(key,tr))+'</h1>'+prose('intro')+prose('basis')+prose('matching')+prose('coverage')
    body+='<p>'+tr.html('report_asof')+': '+value('day',d['day'],'text')+'; '+tr.html('report_previous')+': '+value('before_day',d['before_day'],'text')+'</p>'
    body+='<p>'+tr.html('report_history')+': '+(value('first_day',d['first_day'],'text') if d['first_day'] else tr.html('not_observed'))+'</p>'
    for field in ('published','updated'):
        if d[field]:body+='<p>'+tr.html('report_'+field)+': '+value(field,d[field],'text')+'</p>'
    if not d['indexable']:body+=prose('thin')
    if not d['baseline']:body+=prose('short')
    body+='<h2>'+tr.html('report_counts')+'</h2>'+prose('counts_note')
    body+=rows(['report_period','report_offers','report_providers'],[
        [tr.html('report_now'),value('offers',d['offers']),value('providers',d['providers'])],
        [tr.html('report_previous'),value('before_offers',d['before_offers']),value('before_providers',d['before_providers'])] if d['baseline'] else
        [tr.html('report_previous'),tr.html('not_observed'),tr.html('not_observed')]])
    headers=['gpu_model','price_type','provider','region','report_now']
    body+='<h2>'+tr.html('report_minima')+'</h2>'
    body+=rows(headers+['spec_conditions'],[quote(r)+[qvalue('minima',i,'price_per_gpu_hour',r['price_per_gpu_hour']),conditions('minima',i,r)] for i,r in enumerate(d['minima'])]) if d['minima'] else prose('empty')
    for kind in ('on_demand','spot'):
        if not any(r['price_type']==kind for r in d['minima']):
            body+='<p data-search="report:no_type:'+kind+'">'+tr.html(kind)+': '+tr.html('not_observed')+'</p>'
    for section in ('comparisons','moves'):
        body+='<h2>'+tr.html('report_'+section)+'</h2>'
        body+=rows(headers+['report_previous','report_change','spec_conditions'],[
            quote(p['now'])+[qvalue(section,i,'now/price_per_gpu_hour',p['now']['price_per_gpu_hour']),
                            qvalue(section,i,'before/price_per_gpu_hour',p['before']['price_per_gpu_hour']),
                            qvalue(section,i,'change',p['change'],'percent'),conditions(section,i,p['now'],p['before'])]
            for i,p in enumerate(d[section])]) if d[section] else prose('no_'+section)
    body+='<h2>'+tr.html('report_listings')+'</h2>'+prose('listings_note')
    for field in ('new_models','missing_models','new_providers','missing_providers'):
        body+='<h3>'+tr.html('report_'+field)+'</h3><ul data-search="report:'+field+'">'+(''.join('<li>'+(model(v) if field.endswith('models') else provider(v))+'</li>' for v in d[field]) or '<li>'+tr.html('report_none' if d['baseline'] and d['offers'] else 'not_observed')+'</li>')+'</ul>'
    body+='<h2>'+tr.html('report_related')+'</h2><ul>'+''.join('<li>'+link('/guides/'+s+'/',tr.text('guide_'+k+'_title'))+'</li>' for s,(k,_) in GUIDES.items())+'</ul></article>'
    return title(key,tr),tr.text('report_intro'),body,d['indexable']


def article(key, d, tr, origin):
    url=origin+tr.language.path('/reports/'+key+'/')
    return {'@context':'https://schema.org','@type':'Article','headline':title(key,tr),
            'description':tr.text('report_intro'),'inLanguage':tr.language.code,'url':url,'mainEntityOfPage':url,
            **({'dateModified':d['updated']} if d['updated'] else {})}


def feed_events(data, day):
    start=(date.fromisoformat(day)-timedelta(days=29)).isoformat()
    return [dict(reason='report',week=k,day=d['day'],indexable=d['indexable'],baseline=d['baseline'])
            for k,d in data.items() if start <= d['day'] <= day and d['minima']]
