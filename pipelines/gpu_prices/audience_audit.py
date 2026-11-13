"""Independent SQL-derived audit for b18, without importing feature renderers."""
from base64 import b64encode
from datetime import date, timedelta
from email.utils import format_datetime
from hashlib import sha256
from html.parser import HTMLParser
import json
from statistics import median
import xml.etree.ElementTree as ET

from .i18n import LANGUAGES, load_catalogs
from .models import POPULAR, popular_rank
from .providers.registry import PROVIDERS
from .schema import parse_utc


class Tags(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.tags=[]
        self.feed(raw)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag,dict(attrs)))


def _notices(facts):
    from .audit import _eligible
    notices=[]
    for model,data in facts['exports'].items():
        for kind,points in data['history'].items():
            ordered=sorted(points,key=lambda p:p['day'])
            for i,point in enumerate(ordered):
                if not i or point['day']>facts['date'] or (date.fromisoformat(facts['date'])-date.fromisoformat(point['day'])).days>29:
                    continue
                previous=ordered[i-1]
                if (date.fromisoformat(point['day'])-date.fromisoformat(previous['day'])).days!=1:continue
                reason=None
                if point['min']<=previous['min']*.85:reason='drop'
                elif popular_rank(model)<len(POPULAR) and point['min']<min(p['min'] for p in ordered[:i]):reason='low'
                if reason:notices.append(dict(day=point['day'],reason=reason,model=model,kind=kind,price=point['min'],before=previous['min']))
    first={};days=set()
    active={r['provider'] for d in facts['exports'].values() for r in d['offers'] if _eligible(r)}
    for r in facts['all_rows']:
        if (r['tier']!='A' or not r['export_allowed'] or not _eligible(r) or r['day']>facts['date']
                or parse_utc(r['collected_at'])>parse_utc(facts['built'])):continue
        from .audit import _current
        if not _current(r,r['day']+'T23:59:59.999999Z'):continue
        if r['day']==facts['date'] and r['provider'] not in active:continue
        days.add(r['day']);first[r['provider']]=min(first.get(r['provider'],r['day']),r['day'])
    for provider,day in first.items():
        if 0<=(date.fromisoformat(facts['date'])-date.fromisoformat(day)).days<=29 and (date.fromisoformat(day)-timedelta(days=1)).isoformat() in days:
            notices.append(dict(day=day,reason='provider',provider=provider))
    return sorted(notices,key=lambda n:(-date.fromisoformat(n['day']).toordinal(),json.dumps(n,sort_keys=True)))[:50]


def _feed_expected(items, slugs, origin, tr):
    from .audit import _dollars
    root=ET.Element('rss',version='2.0');channel=ET.SubElement(root,'channel')
    for k,v in [('title',tr.text('growth_feed_title')),('link',origin+tr.language.path('/data/')),('description',tr.text('growth_feed_rules')),('language',tr.language.code)]:
        ET.SubElement(channel,k).text=v
    for item in items:
        node=ET.SubElement(channel,'item');reason=item['reason']
        if reason=='provider':
            title=tr.text('growth_feed_provider',provider=PROVIDERS[item['provider']]['name'],date=item['day'])
            slug='clore-ai' if item['provider']=='clore' else item['provider'].replace('_','-')
            path='/providers/'+slug+'/'
        else:
            title=tr.text('growth_feed_'+reason,model=item['model'],kind=tr.text(item['kind']),price=_dollars(item['price']),before=_dollars(item['before']),date=item['day'])
            path='/'+slugs[item['model']]+'/'
        key=sha256(json.dumps(item,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        for k,v in [('title',title),('link',origin+tr.language.path(path)),('guid',origin+'/feed.xml#'+key),('pubDate',format_datetime(parse_utc(item['day']+'T00:00:00Z'),usegmt=True)),('description',title+' '+tr.text('growth_feed_limits'))]:
            child=ET.SubElement(node,k);child.text=v
            if k=='guid':child.set('isPermaLink','false')
    return ET.tostring(root,encoding='unicode')


def audit_audience(facts, manifest, out, errors, parsed_main):
    from .audit import Page, _eligible, _rate, _dollars, _resolve, _compare
    catalogs=load_catalogs();slugs=manifest['model_slugs'];origin=manifest['site_base_url']
    extra={'_headers','data/latest.json'};pages=0;checked=0;largest=0;badge_count=0
    expected_index=dict(schema_version=1,observation_day=facts['date'],generated_at=facts['built'],
        license='https://creativecommons.org/licenses/by/4.0/',license_scope='compiled data only; provider rights reserved',
        attribution='GPU Price Index',attribution_url=origin+'/data/',exports=[])
    dates=sorted({stamp[:10] for d in facts['exports'].values() for stamp in [d['last_updated']]+[r['collected_at'] for r in d['offers']]+[p['day'] for series in d['history'].values() for p in series] if stamp})
    expected_index['temporal_coverage']=dates[0]+'/'+dates[-1] if dates else None
    for model in sorted(facts['exports'],key=lambda m:'data/'+slugs[m]+'.json'):
        data=facts['exports'][model]
        expected_index['exports'].append(dict(gpu_model=model,url=origin+'/data/'+slugs[model]+'.json',last_updated=data['last_updated'],generated_at=data['generated_at']))
    try:_compare(json.loads((out/'data/latest.json').read_text(encoding='utf-8')),expected_index,'data/latest.json',errors)
    except (OSError,ValueError):errors.append('data/latest.json: missing/invalid index')
    checked+=len(expected_index['exports'])
    notices=_notices(facts)
    main_files={lang.path('/').lstrip('/')+'index.html' for lang in LANGUAGES}
    main_files.update(lang.path('/'+slug+'/').lstrip('/')+'index.html' for lang in LANGUAGES for slug in slugs.values())
    for filename,(_,page) in parsed_main.items():
        if filename not in main_files and any('data-growth' in a for _,a,_ in page.growth_nodes):errors.append(filename+': unexpected growth marker')
    css=("html{color-scheme:light dark;font:14px system-ui,sans-serif}"
         "body{margin:0;padding:16px;overflow-wrap:anywhere}h1{font-size:18px;margin:0 0 10px}"
         "p{margin:8px 0}.price{font-size:28px;font-weight:700}a{color:LinkText}"
         "small{display:block;line-height:1.5}")
    digest=b64encode(sha256(css.encode()).digest()).decode()
    headers=("/embed/*\n  ! X-Frame-Options\n  X-Robots-Tag: noindex\n  Referrer-Policy: no-referrer\n  X-Content-Type-Options: nosniff\n"
             "  Content-Security-Policy: default-src 'none'; style-src 'sha256-"+digest+"'; base-uri 'none'; form-action 'none'; frame-ancestors *\n")
    try:
        if (out/'_headers').read_text(encoding='utf-8')!=headers:errors.append('_headers: embed-only policy mismatch')
    except OSError:errors.append('_headers: missing')
    for lang in LANGUAGES:
        tr=catalogs[lang.code]
        feedfile=lang.path('/feed.xml').lstrip('/');extra.add(feedfile)
        try:
            actual=ET.tostring(ET.parse(out/feedfile).getroot(),encoding='unicode')
            if actual!=_feed_expected(notices,slugs,origin,tr):errors.append(feedfile+': daily feed evidence/order/content mismatch')
        except (OSError,ET.ParseError):errors.append(feedfile+': missing/invalid RSS')
        datafile=lang.path('/data/').lstrip('/')+'index.html'
        try:
            raw=(out/datafile).read_text(encoding='utf-8');page=Page(raw)
            required=('growth_data_title','growth_data_intro','growth_cadence','growth_license','growth_attribution',
                      'growth_fields_title','growth_fields_top','growth_fields_prices','growth_fields_hardware',
                      'growth_fields_policy','growth_fields_time','growth_fields_stats','growth_exports_title','growth_feed_rules')
            if any(tr.text(k) not in page.text for k in required):errors.append(datafile+': missing data documentation/rights qualifiers')
            links=[a for tag,a in page.links if tag=='a']
            found=[a['href'] for a in links if a.get('href','').startswith(origin+'/data/') and a.get('href','').endswith('.json')]
            if found!=[r['url'] for r in expected_index['exports']]:errors.append(datafile+': export list mismatch')
            if not any(a.get('href')==expected_index['license'] and a.get('rel')=='license noopener' for a in links):errors.append(datafile+': missing licence link')
            if not any(a.get('href')=='/data/latest.json' for a in links):errors.append(datafile+': missing latest index')
            # Every public Offer field is documented, never a B/C schema preview.
            from .audit import FIELDS
            if any(field not in page.text for field in FIELDS):errors.append(datafile+': undocumented export field')
            temporal=expected_index['temporal_coverage']
            expected_rows=[('p',{'data-growth':'creator'},tr.text('seo_creator')+': GPU Price Index'),
                           ('p',{'data-growth':'temporal'},tr.text('seo_temporal')+': '+(temporal or tr.text('not_observed')))]
            expected_rows += [('li',{'data-growth':'export'},r['url']+' · '+r['last_updated']) for r in expected_index['exports']]
            if [n for n in page.growth_nodes if 'data-growth' in n[1]]!=expected_rows:errors.append(datafile+': export observation timestamps mismatch')
            expected_dataset={'@context':'https://schema.org','@type':'Dataset','@id':origin+'/data/#dataset',
                'url':origin+lang.path('/data/'),'name':tr.text('growth_data_title'),
                'description':tr.text('growth_data_intro')+' '+tr.text('growth_license'),
                'license':expected_index['license'],'inLanguage':lang.code,
                'creator':{'@type':'Organization','name':'GPU Price Index','url':origin+'/'},
                'temporalCoverage':temporal,'distribution':[{'@type':'DataDownload','encodingFormat':'application/json','contentUrl':r['url']} for r in expected_index['exports']]}
            datasets=[d for d in page.structured if d.get('@type')=='Dataset']
            _compare(datasets,[expected_dataset] if expected_index['exports'] else [],datafile+'/Dataset',errors)
            if not any(a.get('href')==origin+'/' for a in links):errors.append(datafile+': missing visible compilation creator link')
            if any('hidden' in a or 'style' in a for _,a in Tags(raw).tags):errors.append(datafile+': hidden data documentation')
        except (OSError,ValueError):errors.append(datafile+': invalid data page')
        homefile=lang.path('/').lstrip('/')+'index.html'
        home=parsed_main[homefile][1]
        expected_badges=[]
        wanted_days={(date.fromisoformat(facts['date'])-timedelta(days=i)).isoformat() for i in range(1,8)}
        for card in home.cards:
            if card['attrs'].get('data-list')!='cheapest':continue
            row=_resolve(facts,card['attrs']['data-offer']);model=row['gpu_model'];kind=row['price_type']
            points=facts['models'][model]['history'].get(kind,[])
            baseline=[p['min'] for p in points if p['day'] in wanted_days]
            now=[p['min'] for p in points if p['day']==facts['date']]
            if len(baseline)==7 and now==[_rate(row)] and now[0]<=median(baseline)*.9:
                expected_badges.append(('span',{'class':'tag','data-growth':'drop','data-model':model,'data-kind':kind},tr.text('growth_drop')))
        actual=[n for n in home.growth_nodes if 'data-growth' in n[1]]
        if actual!=expected_badges:errors.append(homefile+': price-drop badge evidence/coverage mismatch')
        badge_count+=len(expected_badges)
        for model,data in facts['models'].items():
            slug=slugs[model];filename='embed/'+(lang.prefix+'/' if lang.prefix else '')+slug+'.html'
            extra.add(filename);pages+=1
            model_file=lang.path('/'+slug+'/').lstrip('/')+'index.html'
            main=parsed_main[model_file][1]
            source=origin+'/'+filename
            snippet=('<iframe src="'+source+'" title="'+tr.html('growth_embed_title')+'" width="360" height="520" loading="lazy" referrerpolicy="no-referrer" sandbox="allow-popups allow-popups-to-escape-sandbox"></iframe>')
            textareas=[n for n in main.growth_nodes if n[0]=='textarea']
            if len(textareas)!=1 or textareas[0][2]!=snippet or 'readonly' not in textareas[0][1] or textareas[0][1].get('aria-label')!=tr.text('growth_embed_code'):
                errors.append(model_file+': copyable iframe code mismatch')
            sections=[n for n in main.growth_nodes if n[1].get('data-growth')=='embed']
            text=tr.text('growth_embed_heading')+tr.text('growth_embed_help')+snippet+tr.text('growth_embed_preview')
            if len(sections)!=1 or sections[0][1]!={'data-growth':'embed'} or sections[0][2]!=text or any(n[1].get('data-growth') not in {None,'embed'} for n in main.growth_nodes):errors.append(model_file+': embed section mismatch')
            try:
                raw=(out/filename).read_text(encoding='utf-8');widget=Page(raw);tags=Tags(raw).tags
                size=len(raw.encode());largest=max(largest,size)
                good=[r for r in data['offers'] if r['tier']=='A' and r['export_allowed'] and _eligible(r)]
                row=min(good,key=lambda r:(_rate(r),r['provider'],r['region'] or '',r['sku'],r['price_type']),default=None)
                visible=model+tr.text('growth_embed_basis')
                if row:
                    visible+=_dollars(_rate(row))+tr.text('per_gpu_hour')+' · '.join([PROVIDERS[row['provider']]['name'],tr.text(row['price_type']),row['region'] or tr.text('unknown_region'),tr.text('tax_'+row['tax_basis']),tr.text('availability')+': '+tr.text(row['availability'])])
                    if row['provider']=='clore':visible+=tr.text('community_marketplace')+' · '+tr.text('clore_price_note')
                    if row['original_currency'] and row['original_currency']!=row['currency']:visible+=f"{row['original_currency']} {row['original_price']:g} / {row['price_scope']} / hr · ECB {row['fx_date']}"
                    visible+=tr.text('growth_observed',stamp=row['collected_at'])
                else:visible+=tr.text('growth_embed_empty')
                visible+=tr.text('growth_quote_limits')+tr.text('growth_via')
                links=[a for tag,a in tags if tag=='a']
                styles=[n[2] for n in widget.growth_nodes if n[0]=='style']
                metas=[a for tag,a in tags if tag=='meta']
                valid_attrs=all((a=={'lang':lang.code} if tag=='html' else
                    a in ({},{'class':'price'}) if tag=='p' else not a)
                    for tag,a in tags if tag not in {'a','meta'})
                if (widget.text.removesuffix('\n')!=visible or widget.h1!=[model] or widget.title!=model+' · '+tr.text('site_name')
                    or widget.metadata.get('robots')!='noindex' or size>=15_000 or styles!=[css]
                    or not valid_attrs or metas!=[{'charset':'utf-8'},{'name':'viewport','content':'width=device-width,initial-scale=1'},{'name':'robots','content':'noindex'}]
                    or links!=[{'href':origin+lang.path('/'+slug+'/'),'target':'_blank','rel':'nofollow noopener'}]
                    or any(tag not in {'html','head','meta','title','style','body','h1','p','small','a'} or 'style' in a or 'hidden' in a or any(k.startswith('on') for k in a) for tag,a in tags)):
                    errors.append(filename+': widget price/labels/security/size mismatch')
                checked+=1
            except (OSError,ValueError):errors.append(filename+': missing/invalid embed')
    return extra,dict(extra_html_pages=pages,additional_values_checked=checked,embed_pages=pages,
        max_embed_bytes=largest,feed_items=len(notices),price_drop_badges=badge_count,data_indexes=1)
