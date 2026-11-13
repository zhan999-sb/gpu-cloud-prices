"""Reconstruct weekly evidence from the SQL audit view, never feature helpers."""
from datetime import date, timedelta
from html import unescape
from pathlib import Path
import re

from .models import POPULAR, popular_rank
from .providers.registry import PROVIDERS
from .guide_specs import GUIDES


def _week(day):
    iso=date.fromisoformat(day).isocalendar()
    return '%04d-W%02d' % (iso.year,iso.week)


def snapshot(facts):
    from .audit import _eligible, _current, _rate
    from .schema import parse_utc
    end=facts['date']
    observed=[r for r in facts['all_rows'] if r['day']<=end and parse_utc(r['collected_at'])<=parse_utc(facts['built'])]
    days={}
    for r in observed:
        if r['day']<end and _eligible(r) and _current(r,r['day']+'T23:59:59.999999Z'):
            days.setdefault(r['day'],[]).append({**r,'price_per_gpu_hour':_rate(r)})
    days[end]=[{**r,'price_per_gpu_hour':_rate(r)} for d in facts['models'].values() for r in d['offers'] if _eligible(r)]
    weeks={_week(r['day']):r['day'] for r in sorted(observed,key=lambda r:r['day'])}
    weeks[_week(end)]=end
    output={}
    # Deliberately local: a renderer identity change cannot redefine the audit.
    fields='provider gpu_model gpu_count gpu_fraction price_type region currency price_scope tax_basis billing_unit minimum_billing_seconds original_currency price_conditions source_billing_unit price_basis'.split()
    def rank(r):
        return (popular_rank(r['gpu_model']),r['gpu_model'],r['price_type'],r['price_per_gpu_hour'],r['provider'],r['region'] or '',r['sku'])
    def group(population):
        out={}
        for r in sorted(population,key=lambda r:(r['price_per_gpu_hour'],r['sku'])):
            out.setdefault(tuple(r.get(k) or None if k=='region' else r.get(k) for k in fields),r)
        return out
    for key,day in sorted(weeks.items(),reverse=True):
        prev=(date.fromisoformat(day)-timedelta(days=7)).isoformat()
        active,baseline=days.get(day,[]),days.get(prev,[])
        a,b=group(active),group(baseline)
        selected={}
        for r in sorted(active,key=rank):
            identity=tuple(r.get(k) or None if k=='region' else r.get(k) for k in fields)
            if identity not in b or popular_rank(r['gpu_model'])>=len(POPULAR):continue
            r=a[identity];old=b[identity]
            selected.setdefault((r['gpu_model'],r['price_type']),dict(now=r,before=old,change=(r['price_per_gpu_hour']/old['price_per_gpu_hour']-1)*100))
        comparisons=list(selected.values())
        cheapest={}
        for r in sorted(active,key=rank):
            if popular_rank(r['gpu_model'])<len(POPULAR):cheapest.setdefault((r['gpu_model'],r['price_type']),r)
        stamps=[r['collected_at'] for r in observed if _week(r['day'])==key and r['day']<=day]
        data=dict(week=key,day=day,before_day=prev,first_day=min((r['day'] for r in observed),default=None),
            offers=len(active),providers=len({r['provider'] for r in active}),before_offers=len(baseline),
            before_providers=len({r['provider'] for r in baseline}),current=day==end,baseline=bool(baseline),
            minima=list(cheapest.values()),comparisons=comparisons,
            moves=sorted((p for p in comparisons if p['change']!=0),key=lambda p:(-abs(p['change']),rank(p['now'])))[:10],
            indexable=len(comparisons)>=3,published=min(stamps,default=None),updated=max(stamps,default=None))
        for field,label in [('gpu_model','models'),('provider','providers')]:
            now,old={r[field] for r in active},{r[field] for r in baseline}
            data['new_'+label]=sorted(now-old) if active and baseline else []
            data['missing_'+label]=sorted(old-now) if active and baseline else []
        output[key]=data
    return output


def feed_events(data,day):
    cutoff=(date.fromisoformat(day)-timedelta(days=29)).isoformat()
    return [dict(reason='report',week=k,day=d['day'],indexable=d['indexable'],baseline=d['baseline'])
            for k,d in data.items() if d['minima'] and cutoff<=d['day']<=day]


def audit_page(page,raw,key,data,facts,tr,origin,slugs,filename,errors):
    from .audit import _dollars, _allowed_literals
    from .audience_audit import Tags
    title=tr.text('report_title',week=key) if key else tr.text('report_index_title')
    description=tr.text('report_intro' if key else 'report_index_intro')
    expected_search={};expected_rows=[];values=[];links=set();link_order=[]
    def prose(name):expected_search['report:'+name]=tr.text('report_'+name)
    def record(field,v,fmt='number'):
        text=_dollars(v) if fmt=='money' else format(v,'.2f')+'%' if fmt=='percent' else format(v,'g') if fmt=='number' else str(v)
        values.append(('/reports/'+key+'/'+field,fmt,text))
        return text
    def provider(k):return PROVIDERS[k]['name']
    def provider_route(k):return '/providers/'+('clore-ai' if k=='clore' else k.replace('_','-'))+'/'
    def add_quote(row):
        local=[tr.language.path('/'+slugs[row['gpu_model']]+'/'),tr.language.path(provider_route(row['provider']))]
        links.update(local);link_order.extend(local)
        return [row['gpu_model'],tr.text(row['price_type']),provider(row['provider']),row['region'] or tr.text('unknown_region')]
    def condition_text(r):
        parts=[tr.text('gpu_count')+': '+format(r['gpu_count'],'g'),tr.text('tax_'+r['tax_basis']),
               tr.text({'A':'official_api','B':'tier_b','C':'tier_c'}[r['tier']]),tr.text('scope_'+r['price_scope']),
               tr.text('billing_'+r['billing_unit'].replace('-','_')),tr.text('availability')+': '+tr.text(r['availability'])]
        if r['minimum_billing_seconds'] is not None:parts.append(tr.text('minimum_billing')+': '+str(r['minimum_billing_seconds'])+' '+tr.text('seo_seconds'))
        if r['provider']=='clore':parts.append(tr.text('clore_price_note'))
        if r.get('price_conditions'):parts.append(r['price_conditions'])
        if r.get('original_currency') and r['original_currency']!='USD':
            parts.extend([tr.text('original_hourly')+': '+r['original_currency']+' '+format(r['original_price'],'g'),tr.text('fx_rate_label')+': '+format(r['fx_rate'],'g')+'; ECB '+r['fx_date']])
        parts.append(tr.text('report_asof')+': '+r['collected_at'])
        return ' · '.join(parts)
    def conditions(section,i,r,old=None):
        text=tr.text('report_now')+': '+condition_text(r)
        if old:text+='; '+tr.text('report_previous')+': '+condition_text(old)
        sources=[r]+([old] if old else [])
        text+=' · '+(' · '.join(tr.text('source') for _ in sources))
        links.update(r['source_url'] for r in sources);link_order.extend(r['source_url'] for r in sources)
        expected_search['report:quote:'+section+':'+str(i)]=text
        return text
    if key:
        d=data[key]
        for name in ['intro','basis','matching','coverage']:prose(name)
        for field in ['day','before_day']:
            record(field,d[field],'text')
        if d['first_day']:record('first_day',d['first_day'],'text')
        for field in ['published','updated']:
            if d[field]:record(field,d[field],'text')
        if not d['indexable']:prose('thin')
        if not d['baseline']:prose('short')
        prose('counts_note')
        expected_rows.append([tr.text('report_now'),record('offers',d['offers']),record('providers',d['providers'])])
        expected_rows.append([tr.text('report_previous'),record('before_offers',d['before_offers']),record('before_providers',d['before_providers'])] if d['baseline'] else [tr.text('report_previous'),tr.text('not_observed'),tr.text('not_observed')])
        for i,r in enumerate(d['minima']):
            expected_rows.append(add_quote(r)+[record('minima/'+str(i)+'/price_per_gpu_hour',r['price_per_gpu_hour'],'money'),conditions('minima',i,r)])
        if not d['minima']:prose('empty')
        for kind in ('on_demand','spot'):
            if not any(r['price_type']==kind for r in d['minima']):expected_search['report:no_type:'+kind]=tr.text(kind)+': '+tr.text('not_observed')
        for section in ['comparisons','moves']:
            for i,p in enumerate(d[section]):
                prefix=section+'/'+str(i)+'/'
                expected_rows.append(add_quote(p['now'])+[
                    record(prefix+'now/price_per_gpu_hour',p['now']['price_per_gpu_hour'],'money'),
                    record(prefix+'before/price_per_gpu_hour',p['before']['price_per_gpu_hour'],'money'),
                    record(prefix+'change',p['change'],'percent'),conditions(section,i,p['now'],p['before'])])
            if not d[section]:prose('no_'+section)
        prose('listings_note')
        for field in ['new_models','missing_models','new_providers','missing_providers']:
            expected_search['report:'+field]=''.join(v if field.endswith('models') else provider(v) for v in d[field]) or tr.text('report_none' if d['baseline'] and d['offers'] else 'not_observed')
            for v in d[field]:
                target=tr.language.path('/'+slugs[v]+'/') if field.endswith('models') else tr.language.path(provider_route(v))
                links.add(target);link_order.append(target)
        links.update(tr.language.path('/guides/'+slug+'/') for slug in GUIDES)
        link_order.extend(tr.language.path('/guides/'+slug+'/') for slug in GUIDES)
        url=origin+tr.language.path('/reports/'+key+'/')
        expected_article={'@context':'https://schema.org','@type':'Article','headline':title,'description':description,
            'inLanguage':tr.language.code,'url':url,'mainEntityOfPage':url,
            **({'dateModified':d['updated']} if d['updated'] else {})}
        if [s for s in page.structured if s.get('@type')=='Article']!=[expected_article]:errors.append(filename+': report Article differs from visible content')
        # Every table, caption and header is derived; no fabricated adjacent labels.
        headers=['report_period','report_offers','report_providers']
        tables=1
        for section in ['minima','comparisons','moves']:
            if not d[section]:continue
            tables+=1
            headers+=['gpu_model','price_type','provider','region','report_now']+([] if section=='minima' else ['report_previous','report_change'])+['spec_conditions']
        actual_headers=[unescape(re.sub('<[^>]+>','',s)) for s in re.findall(r'<th\b[^>]*>(.*?)</th>',raw,re.S)]
        captions=[unescape(re.sub('<[^>]+>','',s)) for s in re.findall(r'<caption\b[^>]*>(.*?)</caption>',raw,re.S)]
        accessible_labels=[a.get('aria-label') for tag,a in Tags(raw).tags if tag=='div' and a.get('class')=='table-wrap']
        if (actual_headers!=[tr.text(k) for k in headers] or captions!=[tr.text('report_units')]*tables
                or accessible_labels!=[tr.text('report_units')]*tables):errors.append(filename+': report units/headers mismatch')
    else:
        prose('index_intro')
        for k,d in data.items():
            expected_search['report:status:'+k]=tr.text('report_ready' if d['indexable'] else 'report_thin')
            links.add(tr.language.path('/reports/'+k+'/'))
            link_order.append(tr.language.path('/reports/'+k+'/'))
        if any(s.get('@type')=='Article' for s in page.structured):errors.append(filename+': index must not claim to be an Article')
    if page.table_rows!=expected_rows:errors.append(filename+': report table identity/order/prices/counts mismatch')
    if [v for v in page.values if v[0].startswith('/reports/')]!=values:errors.append(filename+': report numeric pointers mismatch')
    modified=data[key]['updated'] if key else max((d['updated'] for d in data.values() if d['updated']),default=None)
    permitted=_allowed_literals(tr,facts)|{title}
    permitted.update(tr.text('report_title',week=k) for k in data)
    for p,fmt,text in page.values:
        if p.startswith('/reports/'):continue
        if (p,fmt) not in {('/page_updated','date'),('/built','date')}:
            errors.append(filename+': unexpected report statistic '+p);continue
        v=modified if p=='/page_updated' else facts['built']
        if not v or text!=v[:10]:errors.append(filename+': report footer date mismatch')
    if modified and ('/page_updated','date',modified[:10]) not in page.values:errors.append(filename+': report update missing')
    if page.search_values!=expected_search:errors.append(filename+': report prose/listings/status mismatch')
    if any(text not in permitted for text in page.unmarked):errors.append(filename+': unaudited numeric report text')
    if not links<={a.get('href') for tag,a in page.links if tag=='a'}:errors.append(filename+': report internal link missing')
    content=re.search(r'<(?:article|section) data-report="'+(key or 'index')+r'">(.*?)</(?:article|section)>',raw,re.S)
    if not content or [a.get('href') for tag,a in Tags(content.group(1)).tags if tag=='a']!=link_order:errors.append(filename+': report row/source/list link identity mismatch')
    if page.h1!=[title] or page.title!=tr.text('page_title',title=title,site_name=tr.text('site_name')):errors.append(filename+': report title mismatch')
    for field,v in {'description':description,'og:description':description,'twitter:description':description,'og:title':title,'twitter:title':title}.items():
        if page.metadata.get(field)!=v:errors.append(filename+': report metadata mismatch '+field)
    visible=unescape(re.sub('<[^>]+>',' ',re.sub(r'<(script|style)\b[^>]*>.*?</\1>',' ',raw,flags=re.S|re.I)))
    if re.search(r'\bAI\b',visible):errors.append(filename+': forbidden report wording')
    tags=Tags(raw).tags
    if any('hidden' in a or 'style' in a or a.get('aria-hidden')=='true' or any(k.lower().startswith('on') for k in a) for tag,a in tags):errors.append(filename+': report content hidden or executable')
    if any(tag=='script' and (a.get('type')!='application/ld+json' or 'src' in a) for tag,a in tags) or page.metadata.get('refresh'):errors.append(filename+': executable report')
    css=Path(__file__).with_name('design.css').read_text(encoding='utf-8')
    css+='\n[data-growth="embed"] textarea{display:block;width:100%;max-width:100%;box-sizing:border-box;resize:vertical;font:13px/1.5 ui-monospace,monospace;padding:10px;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:8px}\n'
    if [n[2] for n in page.growth_nodes if n[0]=='style']!=[css] or any(tag=='link' and a.get('rel')=='stylesheet' for tag,a in tags):errors.append(filename+': unaudited report stylesheet')
    if raw.count('data-report="'+(key or 'index')+'"')!=1:errors.append(filename+': report article missing')
    return expected_search,title,description
