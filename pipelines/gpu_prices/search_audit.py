"""Independent search/SEO gate over the audit's SQL snapshot.

Never imports the renderer or search content/statistics helpers. Only reviewed
manufacturer constants and language catalogs are shared. No manifest prices
are accepted as expected values.
"""
from datetime import date, timedelta
import math
import hashlib
import json
import re
import struct
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET
import zlib

from .i18n import LANGUAGES, load_catalogs
from .providers.registry import PROVIDERS
from .search_specs import PAIRS, SPECS, REFERENCES
from .guide_specs import GUIDES


def model_copy(model, data, tr):
    from .audit import _eligible, _rate, _dollars, _distribution
    good=[o for o in data['offers'] if _eligible(o)]
    updated=max((r['collected_at'] for r in good),default=None)
    title=tr.text('search_title' if updated else 'search_empty_title',model=model,
                  date=(updated or data['last_updated'] or data['observation_day'])[:10])
    if model=='RTX 4090 24GB':title=tr.text('seo_4090_title')
    elif re.fullmatch(r'(H100|A100)(?: .*GB)?',model):title=tr.text('seo_h100_title' if model.startswith('H100') else 'seo_a100_title',model=model)
    spread=_distribution(good)
    intro=(tr.text('search_intro',minimum=_dollars(spread['min']),median=_dollars(spread['median']),
        offers=len(good),providers=len({o['provider'] for o in good}),
        demand=sum(r['price_type']=='on_demand' for r in good),spot=sum(r['price_type']=='spot' for r in good))
        if good else tr.text('search_empty_intro'))
    end=date.fromisoformat(data['observation_day']);start=end-timedelta(days=29)
    trends=[]
    for kind,series in data['history'].items():
        points=sorted((p for p in series if start<=date.fromisoformat(p['day'])<=end),key=lambda p:p['day'])
        if len(points)>=2:
            change=100*(points[-1]['min']-points[0]['min'])/points[0]['min']
            # Round after independent arithmetic, with a stable signed zero.
            trends.append(tr.text('search_trend',kind=tr.text(kind),first=points[0]['day'],last=points[-1]['day'],change=format(change,'+.2f')+'%',days=len(points)))
    return title,intro,' '.join(trends) if trends else tr.text('search_trend_wait')


def _family(models, family):
    # Independent membership sets. Unspecified interfaces remain separate rows;
    # laptops, D variants and modified capacities never join a desktop pair.
    names={
        'H100': {'H100 SXM 80GB','H100 PCIe 80GB','H100 NVL 94GB'},
        'A100': {f'A100{interface} {size}GB' for interface in ('',' SXM4',' SXM',' PCIe') for size in (40,80)},
        'H200': {'H200 141GB','H200 SXM5 141GB'}, 'L40S': {'L40S 48GB'},
        'RTX 4090': {'RTX 4090 24GB'}, 'RTX 5090': {'RTX 5090 32GB'}}
    return sorted(set(models)&names[family])


def _entries(facts):
    return [('/models/'+m.replace('~','~0').replace('/','~1')+'/offers/'+str(i),r)
            for m,d in facts['models'].items() for i,r in enumerate(d['offers'])]


def _order(items):
    from .audit import _rate
    return sorted(items,key=lambda e:(e[1]['currency']!='USD' or e[1]['price_scope']=='gpu_component' or _rate(e[1]) is None,
        e[1]['currency'],_rate(e[1]) if _rate(e[1]) is not None else e[1]['price_per_hour'],e[0]))


def _provider_slug(key):
    return 'clore-ai' if key=='clore' else key.replace('_','-')


def _file(route, lang):
    path=lang.path(route).lstrip('/')
    return path+'index.html' if not path or path.endswith('/') else path if path.endswith('.html') else path+'.html'


def _png(raw):
    """Validate real PNG CRCs, dimensions, scanline shape and lossless payload."""
    if raw[:8]!=b'\x89PNG\r\n\x1a\n':raise ValueError('PNG signature')
    pos=8;payload=b'';kinds=[]
    while pos<len(raw):
        n=struct.unpack('>I',raw[pos:pos+4])[0];kind=raw[pos+4:pos+8];body=raw[pos+8:pos+8+n]
        if zlib.crc32(kind+body)!=struct.unpack('>I',raw[pos+8+n:pos+12+n])[0]:raise ValueError('PNG CRC')
        if kind==b'IHDR' and body!=struct.pack('>IIBBBBB',1200,630,8,2,0,0,0):raise ValueError('PNG format')
        if kind==b'IDAT':payload+=body
        kinds.append(kind);pos+=12+n
    if kinds!=[b'IHDR',b'IDAT',b'IEND'] or pos!=len(raw):raise ValueError('PNG chunks')
    pixels=zlib.decompress(payload)
    if len(pixels)!=630*(1200*3+1) or any(pixels[i*(3601)]!=0 for i in range(630)):raise ValueError('PNG scanlines')
    return pixels


def audit_search(facts, manifest, out, errors, *, parsed_main=None):
    from .audit import Page, _eligible, _rate, _dollars, _resolve, _compare, _allowed_literals, _audit_cards
    catalogs=load_catalogs();origin=manifest['site_base_url'];slugs=manifest['model_slugs']
    entries=_entries(facts);extra=set();routes={};indexed={};parsed={}
    from .guide_audit import snapshot as guide_snapshot, audit_page as audit_guide_page
    guide_data=guide_snapshot(facts,errors)
    from .report_audit import snapshot as report_snapshot, audit_page as audit_report_page, _week
    report_data=report_snapshot(facts)
    day=facts['date'];last=facts['last_updated'] or facts['built']
    from .schema import parse_utc
    tracked=sorted({r['provider'] for r in facts['all_rows'] if parse_utc(r['collected_at'])<=parse_utc(facts['built'])})
    # (kind, subject, eligible count, lastmod). Compare pages require >=3 on
    # each side, stricter than merely combining two thin model populations.
    routes['/']=('home',None,facts['summary']['offers'],last)
    routes['/methodology']=('method',None,facts['summary']['offers'],last)
    routes['/404.html']=('404',None,0,last)
    routes['/data/']=('data',None,3 if facts['exports'] else 0,max((d['last_updated'] for d in facts['exports'].values()),default=last))
    for slug in ('',*GUIDES):
        key=GUIDES[slug][0] if slug else 'index'
        routes['/guides/'+(slug+'/' if slug else '')]=('guide',slug,guide_data['counts'][key],guide_data['updated'] or facts['built'])
    for key in (None,*report_data):
        ready=report_data[key]['indexable'] if key else any(d['indexable'] for d in report_data.values())
        modified=report_data[key]['updated'] if key else max((d['updated'] for d in report_data.values() if d['updated']),default=None)
        routes['/reports/'+(key+'/' if key else '')]=('report',key,3 if ready else 0,modified or facts['built'])
    for model,d in facts['models'].items():
        route='/'+slugs[model]+'/'
        routes[route]=('model',model,d['statistics']['offers'],d['last_updated'] or last)
        for number in range(1,(len(d['offers'])+11)//12):
            routes[route+'offers/'+str(number+1)+'/']=('continuation',(model,number),0,d['last_updated'] or last)
    for key in tracked:
        local=[r for _,r in entries if r['provider']==key]
        routes['/providers/'+_provider_slug(key)+'/']=('provider',key,sum(_eligible(r) for r in local),max((r['collected_at'] for r in local),default=last))
    for slug,pair in PAIRS.items():
        counts=[sum(facts['models'][m]['statistics']['offers'] for m in _family(facts['models'],family)) for family in pair]
        routes['/compare/'+slug+'/']=('comparison',slug,min(counts),last)
    extra.update('sitemap-'+l.code+'.xml' for l in LANGUAGES)
    for model in facts['models']:
        filename='social/'+slugs[model]+'.png';extra.add(filename)
        try:
            pixels=_png((out/filename).read_bytes())
            # Reconstruct text glyph masks from an audit-owned alphabet below.
            _audit_image(pixels,model,facts['models'][model],errors,filename)
        except (ValueError,OSError,struct.error,zlib.error):errors.append(filename+': invalid social PNG')
    extra_html=0;additional_values=0
    for lang in LANGUAGES:
        tr=catalogs[lang.code]
        allowed=_allowed_literals(tr,facts)|set(SPECS)
        allowed.update(tr.text('compare_label',left=a,right=b) for a,b in PAIRS.values())
        for route,(kind,subject,count,modified) in routes.items():
            filename=_file(route,lang);is_aux=kind in {'provider','comparison','continuation','data','guide','report'}
            if is_aux:extra.add(filename);extra_html+=1
            try:
                if parsed_main and filename in parsed_main:raw,page=parsed_main[filename]
                else:raw=(out/filename).read_text(encoding='utf-8');page=Page(raw)
            except (OSError,ValueError) as exc:errors.append(filename+': unreadable/invalid HTML '+str(exc));continue
            parsed[origin+lang.path(route)]=page
            additional_values+=len(page.search_values)+(len(page.values) if is_aux else 0)
            url=origin+lang.path(route)
            noindex=count<3
            if (page.metadata.get('robots')=='noindex')!=noindex:errors.append(filename+': thin-content/noindex mismatch')
            if not noindex:indexed[url]=(modified[:10],route,lang.code)
            expected_alts={l.code:origin+l.path(route) for l in LANGUAGES}|{'x-default':origin+route}
            alternates=[a for tag,a in page.links if tag=='link' and a.get('rel')=='alternate' and 'hreflang' in a]
            if len(alternates)!=9 or {a.get('hreflang'):a.get('href') for a in alternates}!=expected_alts:errors.append(filename+': hreflang mismatch')
            feeds=[a for tag,a in page.links if tag=='link' and a.get('rel')=='alternate' and 'hreflang' not in a]
            if feeds!=page.rss_links or feeds!=[{'rel':'alternate','type':'application/rss+xml','title':tr.text('growth_feed_title'),'href':origin+lang.path('/feed.xml')}]:errors.append(filename+': RSS autodiscovery mismatch')
            canonical=[a.get('href') for tag,a in page.links if tag=='link' and a.get('rel')=='canonical']
            if canonical!=[url] or page.metadata.get('og:url')!=url:errors.append(filename+': canonical mismatch')
            if len(page.h1)!=1:errors.append(filename+': expected one H1')
            if any(tag=='script' and a.get('type') not in {'module','application/ld+json'} for tag,a in page.links):errors.append(filename+': blocking JavaScript')
            if kind in {'model','continuation'} and len(raw.encode('utf-8'))>300_000:errors.append(filename+': model HTML over 300 KB')
            expected_search={};expected_cards=None;title=None;description=None
            if kind=='model':
                expected_types=['Dataset' if subject in facts['exports'] else 'WebPage']
                if facts['models'][subject]['offers']:expected_types.append('FAQPage')
                expected_types.append('BreadcrumbList')
            else:
                expected_types=({'home':['WebSite','BreadcrumbList'],'method':['WebPage','BreadcrumbList'],
                                 '404':['WebPage','BreadcrumbList'],
                                 'data':['Dataset','BreadcrumbList'] if facts['exports'] else ['BreadcrumbList']}.get(kind,['BreadcrumbList']))
            if kind=='guide':expected_types=(['Article'] if subject else [])+['BreadcrumbList']
            if kind=='report':expected_types=(['Article'] if subject else [])+['BreadcrumbList']
            if [d.get('@type') for d in page.structured]!=expected_types:
                errors.append(filename+': unexpected JSON-LD types/count (A-only Dataset and visible FAQ required)')
            if kind=='guide':
                expected_search,title,description=audit_guide_page(page,raw,GUIDES[subject][0] if subject else 'index',subject,
                    guide_data,facts,tr,origin,slugs,filename,errors)
            elif kind=='report':
                expected_search,title,description=audit_report_page(page,raw,subject,report_data,facts,tr,origin,slugs,filename,errors)
            elif kind=='model':
                title,intro,trend=model_copy(subject,facts['models'][subject],tr)
                expected_search={'intro':intro,'trend':trend}
                buyer=('seo_4090_intro' if subject=='RTX 4090 24GB' else
                       'seo_h100_intro' if re.fullmatch(r'H100(?: .*GB)?',subject) else
                       'seo_a100_intro' if re.fullmatch(r'A100(?: .*GB)?',subject) else None)
                if buyer:expected_search['buyer-intro']=tr.text(buyer)
                if buyer in {'seo_h100_intro','seo_a100_intro'}:expected_search['variant']=tr.text('seo_variant',model=subject)
                social=origin+'/social/'+slugs[subject]+'.png'
                if page.metadata.get('og:image')!=social or page.metadata.get('twitter:image')!=social or page.metadata.get('twitter:card')!='summary_large_image':errors.append(filename+': social image mismatch')
            elif kind=='provider':
                provider=PROVIDERS[subject];title=tr.text('provider_title',provider=provider['name'],date=day);description=tr.text('provider_intro',provider=provider['name'])
                local=[(p,r) for p,r in entries if r['provider']==subject];good=[e for e in local if _eligible(e[1])]
                names=sorted({r['gpu_model'] for _,r in local})
                expected_search={'provider-counts':tr.text('provider_counts',offers=len(local),eligible=len(good),models=len(names))}
                for m in names:
                    expected_search['count:'+m]=str(sum(r['gpu_model']==m for _,r in local))
                    expected_search['eligible:'+m]=str(sum(r['gpu_model']==m for _,r in good))
                expected_cards=_order(good)[:3]
                hrefs={a.get('href') for tag,a in page.links if tag=='a'}
                if provider['source'] not in hrefs or tr.text(provider['note']) not in page.text or tr.text({'A':'official_api','B':'tier_b','C':'tier_c','D':'link_only'}[provider['tier']]) not in page.text:errors.append(filename+': missing provider source/tier/coverage note')
                if not {lang.path('/'+slugs[m]+'/') for m in names}<=hrefs:errors.append(filename+': missing provider/model link')
                # Check complete model table identity, counts, minima and types.
                expected_rates=[];expected_rows=[]
                for m in names:
                    candidates=[e for e in good if e[1]['gpu_model']==m]
                    quote=tr.text('not_observed')
                    if candidates:
                        p,r=min(candidates,key=lambda e:_rate(e[1]));expected_rates.append((p+'/price_per_gpu_hour','money',_dollars(_rate(r))))
                        quote=_dollars(_rate(r))+' '+tr.text(r['price_type'])
                    expected_rows.append([m,expected_search['count:'+m],expected_search['eligible:'+m],quote])
                if page.table_rows!=expected_rows:errors.append(filename+': provider table labels/types mismatch')
                actual_rates=page.outside_values
                actual_rates=[v for v in actual_rates if '/offers/' in v[0]]
                if actual_rates!=expected_rates:errors.append(filename+': provider cheapest-model table mismatch')
            elif kind=='comparison':
                left,right=PAIRS[subject];title=tr.text('compare_title',left=left,right=right,date=day);description=tr.text('compare_intro')
                expected_cards=[];expected_rates=[];expected_rows=[]
                for family in (left,right):
                    names=_family(facts['models'],family)
                    candidates=[e for e in entries if e[1]['gpu_model'] in names and _eligible(e[1])]
                    expected_cards+=_order(candidates)[:1]
                    for m in names:
                        stem='/models/'+m.replace('~','~0').replace('/','~1')+'/statistics'
                        d=facts['models'][m]['statistics']
                        expected_rates.append((stem+'/offers','number',format(d['offers'],'g')))
                        expected_rates.extend((stem+'/cheapest/'+k,'money',_dollars(d['cheapest'][k])) for k in ('on_demand','spot') if d['cheapest'][k] is not None)
                        expected_rows.append([m,str(d['offers'])]+[_dollars(d['cheapest'][k]) if d['cheapest'][k] is not None else tr.text('not_observed') for k in ('on_demand','spot')])
                actual_rates=[v for v in page.values if '/statistics/' in v[0]]
                if actual_rates!=expected_rates:errors.append(filename+': comparison live-price table mismatch')
                expected_specs=[]
                for f in (left,right):
                    model=REFERENCES[f];spec=SPECS[model]
                    expected_specs.extend(('/specs/'+model+'/'+k,'number',format(spec[k],'g')) for k in ('vram','bandwidth','fp16','bf16'))
                    expected_rows.append([model]+[format(spec[k],'g') for k in ('vram','bandwidth','fp16','bf16')]+[tr.text(spec['condition'])])
                if page.table_rows!=expected_rows:errors.append(filename+': comparison table model labels/conditions mismatch')
                if [v for v in page.values if v[0].startswith('/specs/')]!=expected_specs:errors.append(filename+': official spec table mismatch')
                hrefs={a.get('href') for tag,a in page.links if tag=='a'}
                if not {SPECS[REFERENCES[f]]['source'] for f in (left,right)}<=hrefs:errors.append(filename+': missing official spec citation')
                expected_model_links={lang.path('/'+slugs[m]+'/') for f in (left,right) for m in _family(facts['models'],f)}
                if not expected_model_links<=hrefs:errors.append(filename+': missing comparison/model link')
                if tr.text('spec_note') not in page.text or any(tr.text(SPECS[REFERENCES[f]]['condition']) not in page.text for f in (left,right)):errors.append(filename+': missing spec conditions')
            elif kind=='continuation':
                model,number=subject;title=tr.text('offer_page',model=model,number=number+1);description=tr.text('offer_pages_note')
                expected_cards=_order([e for e in entries if e[1]['gpu_model']==model])[number*12:(number+1)*12]
            elif kind=='data':
                title=tr.text('growth_data_title');description=tr.text('growth_data_intro');expected_cards=[]
            if page.search_values!=expected_search:errors.append(filename+': search copy/counts differ from SQL')
            if title:
                if page.h1!=[title]:errors.append(filename+': H1 differs from SQL metadata')
            else:title=page.metadata.get('og:title')
            crumb_items=[(tr.text('nav_home'),'/')]+([(title,route)] if route!='/' else [])
            if kind=='continuation':crumb_items=[(tr.text('nav_home'),'/'),(subject[0],'/'+slugs[subject[0]]+'/'),(title,route)]
            if kind=='guide':
                crumb_items=[(tr.text('nav_home'),'/'),(tr.text('guide_index_title'),'/guides/')]+([(title,route)] if subject else [])
            if kind=='report':
                crumb_items=[(tr.text('nav_home'),'/'),(tr.text('report_index_title'),'/reports/')]+([(title,route)] if subject else [])
            expected_crumb={'@context':'https://schema.org','@type':'BreadcrumbList','inLanguage':lang.code,'url':url,'itemListElement':[{'@type':'ListItem','position':i+1,'name':label,'item':origin+lang.path(path)} for i,(label,path) in enumerate(crumb_items)]}
            crumbs=[d for d in page.structured if d.get('@type')=='BreadcrumbList']
            if crumbs!=[expected_crumb] or page.breadcrumbs!=[' / '.join(label for label,_ in crumb_items)]:errors.append(filename+': breadcrumb mismatch')
            if is_aux and kind not in {'guide','report'}:
                if kind!='data' and any('data-growth' in a for _,a,_ in page.growth_nodes):errors.append(filename+': unexpected growth marker on auxiliary page')
                if page.title!=tr.text('page_title',title=title,site_name=tr.text('site_name')):errors.append(filename+': auxiliary title mismatch')
                for k,v in {'og:title':title,'twitter:title':title,'description':description,'og:description':description,'twitter:description':description}.items():
                    if page.metadata.get(k)!=v:errors.append(filename+': auxiliary '+k+' mismatch')
                if kind!='data' and any(d.get('@type') in {'Dataset','FAQPage'} for d in page.structured):errors.append(filename+': unexpected dataset/FAQ on auxiliary page')
                _audit_cards(page,subject[0] if kind=='continuation' else 'auxiliary',tr,facts,filename,errors,entries_override=expected_cards)
                permitted=allowed|{title}
                if kind=='data':
                    permitted.add('CC BY 4.0')
                    permitted.update(origin+'/data/'+slugs[m]+'.json' for m in facts['exports'])
                    permitted.update('· '+d['last_updated'] for d in facts['exports'].values())
                    dates=sorted({s[:10] for d in facts['exports'].values() for s in [d['last_updated']]+[r['collected_at'] for r in d['offers']]+[p['day'] for series in d['history'].values() for p in series] if s})
                    permitted.add((dates[0]+'/'+dates[-1]) if dates else tr.text('not_observed'))
                for m in facts['models']:
                    permitted.update(tr.text('offer_page',model=m,number=i+1) for i in range((len(facts['models'][m]['offers'])+11)//12))
                for text in set(page.unmarked):
                    if text not in permitted:errors.append(filename+': unaudited numeric text '+repr(text))
                page_facts={**facts,'specs':SPECS,'page_updated':modified}
                for p,fmt,text in page.values:
                    try:
                        v=_resolve(page_facts,p)
                        if fmt=='money':expected=_dollars(v)
                        elif fmt=='number':expected=format(v,'g')
                        elif fmt.startswith('minimum-charge-'):
                            amount=v['price_per_hour']*(v['gpu_count'] if v['price_scope']=='per_gpu' else 1)*v['minimum_billing_seconds']/3600
                            expected=_dollars(amount) if fmt.endswith('money') else format(amount,'g')
                        elif fmt=='date':expected=v[:10]
                        elif fmt=='clock':expected=__import__('datetime').datetime.fromisoformat(v.replace('Z','+00:00')).strftime('%H:%M UTC')
                        elif fmt.startswith('instance-'):
                            amount=v['price_per_hour']*((v['gpu_count'] or 1) if v['price_scope']=='per_gpu' else 1)
                            expected=_dollars(amount) if fmt=='instance-money' else format(amount,'g')
                        else:expected=str(v)
                        if text!=expected:errors.append(filename+': auxiliary statistic mismatch '+p)
                    except (KeyError,IndexError,ValueError,TypeError):errors.append(filename+': invalid auxiliary statistic '+p)
            # Every main model links to its providers and selected comparisons.
            if kind=='model':
                hrefs={a.get('href') for tag,a in page.links if tag=='a'}
                expected_links={lang.path('/providers/'+_provider_slug(r['provider'])+'/') for r in facts['models'][subject]['offers']}
                expected_links.update(lang.path('/compare/'+slug+'/') for slug,pair in PAIRS.items() if any(subject in _family(facts['models'],f) for f in pair))
                expected_links.update(lang.path('/'+slugs[subject]+'/offers/'+str(n+1)+'/') for n in range(1,(len(facts['models'][subject]['offers'])+11)//12))
                if not expected_links<=hrefs:errors.append(filename+': missing related/continuation links')
            if not any(tag=='a' and a.get('href')==lang.path('/guides/') for tag,a in page.links):
                errors.append(filename+': missing Guides navigation')
            if kind=='home' and not {lang.path('/guides/'+s+'/') for s in GUIDES}<={a.get('href') for tag,a in page.links if tag=='a'}:
                errors.append(filename+': missing home Guides section')
            if kind=='home':
                latest=[a for tag,a in page.links if tag=='a' and a.get('data-report-latest')=='true']
                if latest!=[{'data-report-latest':'true','href':lang.path('/reports/'+_week(day)+'/')}]:errors.append(filename+': latest report link mismatch')
                if lang.path('/reports/') not in {a.get('href') for tag,a in page.links if tag=='a'}:errors.append(filename+': reports index link missing')
    # Exact sitemap membership, lastmod, duplicates and reciprocal hreflang.
    ns={'s':'http://www.sitemaps.org/schemas/sitemap/0.9','x':'http://www.w3.org/1999/xhtml'}
    for filename in ['sitemap.xml']+['sitemap-'+l.code+'.xml' for l in LANGUAGES]:
        code=filename[len('sitemap-'):-4] if filename!='sitemap.xml' else None
        expected={u:info for u,info in indexed.items() if code is None or info[2]==code}
        try:
            root=ET.parse(out/filename).getroot();found={}
            if root.tag!='{'+ns['s']+'}urlset':raise ValueError('urlset required')
            for node in root:
                url=node.findtext('s:loc',namespaces=ns);stamp=node.findtext('s:lastmod',namespaces=ns)
                if url in found or url not in expected:errors.append(filename+': duplicate/unexpected sitemap URL')
                found[url]=stamp
                if url in expected:
                    info=expected[url];alts=node.findall('x:link',ns)
                    if len(alts)!=9 or {a.get('hreflang'):a.get('href') for a in alts}!={l.code:origin+l.path(info[1]) for l in LANGUAGES}|{'x-default':origin+info[1]}:errors.append(filename+': sitemap alternates mismatch')
            if found!={u:info[0] for u,info in expected.items()}:errors.append(filename+': sitemap membership/lastmod mismatch')
        except (ET.ParseError,OSError,ValueError):errors.append(filename+': invalid sitemap')
    expected_robots='User-agent: *\nAllow: /\n\nSitemap: '+origin+'/sitemap.xml\n'+''.join('Sitemap: '+origin+'/sitemap-'+l.code+'.xml\n' for l in LANGUAGES)
    if (out/'robots.txt').read_text(encoding='utf-8')!=expected_robots:errors.append('robots.txt: mismatch')
    # Check every generated local link, language alternate and metadata image.
    for url,page in parsed.items():
        for tag,a in page.links:
            href=a.get('href')
            if not href:continue
            target=urlsplit(urljoin(url,href))
            if target.netloc!=urlsplit(origin).netloc:continue
            path=target.path.lstrip('/')
            path=path+'index.html' if path.endswith('/') or not path else path if '.' in path.rsplit('/',1)[-1] else path+'.html'
            if not (out/path).is_file():errors.append(url+': broken internal link '+href)
    key='d6ef1384259a4c708385f2af990ac9a1'
    extra.update({key+'.txt','indexnow-manifest.json'})
    try:
        if (out/(key+'.txt')).read_bytes()!=(key+'\n').encode():errors.append('IndexNow public key file mismatch')
        expected_hashes={}
        for url,(_,route,code) in indexed.items():
            lang=next(l for l in LANGUAGES if l.code==code)
            raw=(out/_file(route,lang)).read_text(encoding='utf-8')
            raw=re.sub(r'<time datetime="[^"]*" data-stat="/built" data-format="date">[^<]*</time>','<time data-stat="/built"></time>',raw)
            expected_hashes[url]=hashlib.sha256(raw.encode('utf-8')).hexdigest()
        _compare(json.loads((out/'indexnow-manifest.json').read_text(encoding='utf-8')),
                 {'version':1,'origin':origin,'pages':expected_hashes},'IndexNow page hashes',errors)
    except (OSError,ValueError):errors.append('IndexNow key/manifest missing or invalid')
    return extra,dict(extra_html_pages=extra_html,additional_values_checked=additional_values,indexed_pages=len(indexed),noindex_pages=len(parsed)-len(indexed),sitemap_urls=len(indexed),language_sitemaps=8,guide_pages=len(LANGUAGES)*(len(GUIDES)+1),report_pages=len(LANGUAGES)*(len(report_data)+1),indexable_reports=sum(d['indexable'] for d in report_data.values())*len(LANGUAGES))


def _audit_image(pixels, model, data, errors, filename):
    """Independently lay out SQL-derived text and compare every RGB pixel.

    Shared glyphs are declarative shapes, like the shared spec constants;
    neither renderer output nor its formatting/layout helpers are an oracle.
    """
    from .audit import _eligible
    from .bitmap_font import GLYPHS
    good=[r for r in data['offers'] if _eligible(r)]
    stamp=max((r['collected_at'] for r in good),default=None) or data['last_updated'] or data['observation_day']
    expected=bytearray((b'\0'+bytes((16,34,62))*1200)*630)
    lines=[('GPU PRICE INDEX',60,55,6,(131,181,255))]
    lines.extend((model[n:n+30],60,145+(n//30)*65,5,(255,255,255)) for n in range(0,min(len(model),90),30))
    unit=data['currency']+' / GPU-HR' if data['currency']!='MULTI' else 'GPU-HR'
    lines.extend([(unit,60,365,5,(131,181,255)),(stamp[:10],60,450,4,(220,230,245))])
    for text,x,y,scale,color in lines:
        for char_index,c in enumerate(text.upper()):
            mask=GLYPHS.get(c,GLYPHS['?'])
            for row in range(7):
                for col in range(5):
                    if mask[row*5+col]!='1':continue
                    for dy in range(scale):
                        px=x+(char_index*6+col)*scale;py=y+row*scale+dy
                        if 0<=py<630 and 0<=px<1200-scale:
                            start=py*3601+1+px*3
                            expected[start:start+scale*3]=bytes(color)*scale
    if expected!=pixels:errors.append(filename+': social PNG identity/date/units differ from SQL')
