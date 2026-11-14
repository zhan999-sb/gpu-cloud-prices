"""Independent guide arithmetic and content gate over the SQL audit snapshot.

Does not import guides, store, build_site or renderer statistics. Declarative
editorial identities, official source references and translations are shared.
"""
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from html import unescape
from pathlib import Path
import re

from .guide_specs import GUIDES, SOURCES, REFERENCE_MODELS
from .models import popular_rank, POPULAR
from .search_specs import PAIRS


def _middle(values):
    ordered=sorted(values)
    n=len(ordered)
    return (ordered[(n-1)//2]+ordered[n//2])/2


def _paired_percent(demand, spot):
    # Independent rational arithmetic from DB source amounts, not the
    # renderer's Decimal ratio or its stored per-GPU normalization.
    reduction=100*(Fraction(str(demand['price_per_hour']))-Fraction(str(spot['price_per_hour'])))/Fraction(str(demand['price_per_hour']))
    return Decimal(reduction.numerator)/Decimal(reduction.denominator)


def snapshot(facts, errors):
    from .audit import _eligible, _rate
    refs=[(r['model'],r['parameters_billions'],r['source_url']) for r in facts['guide_references']]
    if refs and refs!=list(REFERENCE_MODELS):
        errors.append('guide reference database differs from reviewed official model-card facts')
        refs=[]
    rows=[]
    for model,d in facts['models'].items():
        for i,r in enumerate(d['offers']):
            if _eligible(r):rows.append(('/models/'+model.replace('~','~0').replace('/','~1')+'/offers/'+str(i),r))
    ordered=sorted(rows,key=lambda e:(_rate(e[1]),e[0]))
    memory={}
    for p,r in rows:
        m=re.search(r'\b([0-9]+(?:\.[0-9]+)?)GB$',r['gpu_model'])
        if m and float(m[1])>0:memory[p]=float(m[1])
    bands=[]
    for lo,hi in [(16,24),(24,48),(48,80),(80,None)]:
        for kind in ('on_demand','spot'):
            candidates=[(p,r) for p,r in ordered if p in memory and memory[p]>=lo
                        and (hi is None or memory[p]<hi) and r['price_type']==kind]
            bands.append({'lower':lo,'upper':hi,'kind':kind,'quote':candidates[0] if candidates else None})
    by_model={}
    for p,r in ordered:
        if popular_rank(r['gpu_model'])>=len(POPULAR):continue
        identity=tuple(r[k] for k in ['provider','sku','region','gpu_count','gpu_fraction','price_scope',
                                     'currency','tax_basis','vcpu','ram_gb','minimum_billing_seconds'])
        types=by_model.setdefault(r['gpu_model'],{}).setdefault(identity,{})
        if r['price_type'] not in types:types[r['price_type']]=(p,r)
    discounts=[]
    for model,groups in sorted(by_model.items(),key=lambda e:(popular_rank(e[0]),e[0])):
        pairs=[(v['on_demand'],v['spot']) for v in groups.values() if 'on_demand' in v and 'spot' in v]
        # Match renderer iteration for the illustrative subset, independent of
        # price sorting: original SQL snapshot order defines cohort identity.
        original={p:i for i,(p,r) in enumerate(rows)}
        pairs.sort(key=lambda pair:min(original[pair[0][0]],original[pair[1][0]]))
        if pairs:
            discounts.append(dict(model=model,count=len(pairs),pairs=pairs,
                demand=_middle([_rate(d) for (_,d),s in pairs]),
                spot=_middle([_rate(s) for d,(_,s) in pairs]),
                percent=_middle([_paired_percent(d,s) for (_,d),(_,s) in pairs])))
    multi_models=sorted({r['gpu_model'] for p,r in ordered if r['gpu_count']>1},key=lambda m:(popular_rank(m),
        min((_rate(r),p) for p,r in ordered if r['gpu_model']==m and r['gpu_count']>1)))
    bills=[next(e for e in ordered if e[1]['gpu_model']==m and e[1]['gpu_count']>1) for m in multi_models[:3]]
    bills.extend([e for e in ordered if e not in bills][:max(0,3-len(bills))])
    values=[]
    for model in sorted({r['gpu_model'] for p,r in rows if p in memory},key=lambda m:(popular_rank(m),m)):
        for kind in ('on_demand','spot'):
            candidates=[e for e in ordered if e[0] in memory and e[1]['gpu_model']==model and e[1]['price_type']==kind]
            if candidates:
                p,r=candidates[0];v=memory[p]
                values.append(dict(model=model,kind=kind,vram=v,ratio=_rate(r)/v,quote=(p,r)))
    weights=[dict(model=name,parameters=size,fp16=size*16/8,int4=size*4/8,source=url) for name,size,url in refs]
    return dict(entries=rows,classes=bands,discounts=discounts,billing=bills,values=values,weights=weights,
        counts=dict(index=len(rows),choose=len(memory) if weights else 0,spot=sum(d['count'] for d in discounts),
                    billing=len(rows),glossary=len(rows),value=len(memory)),
        updated=max((r['collected_at'] for _,r in rows),default=None))


def audit_page(page, raw, key, slug, data, facts, tr, origin, slugs, filename, errors, *, include_workloads=True):
    from .audit import _audit_cards, _allowed_literals, _resolve, _dollars, _rate
    title=tr.text('guide_'+key+'_title');description=tr.text('guide_'+key+'_intro')
    names=[key+'_intro','basis']
    if key=='choose':names+=['memory','choose_steps']
    elif key=='spot':names+=['spot_method','interruptions']+(['missing_pairs'] if not data['discounts'] else [])
    elif key=='billing':names+=['billing_math','billing_limits']
    elif key=='glossary':names+=['term_'+s for s in ('demand','spot','reserved','market','tax','from','component','regions')]
    elif key=='value':names+=['value_method']
    selected=[]
    if slug:
        names+=['examples','sources']
        if key=='choose':selected=[c['quote'] for c in data['classes'] if c['quote']]
        elif key=='spot':selected=[e for d in data['discounts'] for pair in d['pairs'] for e in pair][:6]
        elif key=='billing':selected=data['billing']
        elif key=='value':selected=[d['quote'] for d in data['values']]
        elif key=='glossary':selected=sorted(data['entries'],key=lambda e:(_rate(e[1]),e[0]))[:1]
    if data['counts'][key]<3:names+=['thin']
    names+=['related']
    expected_search={'guide:'+s:tr.text('guide_'+s) for s in names}
    if slug:expected_search['guide:source-list']=''.join(SOURCES[s][0] for s in GUIDES[slug][1])
    if page.search_values!=expected_search:errors.append(filename+': guide explanation/source/visibility mismatch')
    if page.h1!=[title]:errors.append(filename+': guide title mismatch')
    for field,value in {'description':description,'og:description':description,'twitter:description':description,
                        'og:title':title,'twitter:title':title}.items():
        if page.metadata.get(field)!=value:errors.append(filename+': guide '+field+' mismatch')
    if page.title!=tr.text('page_title',title=title,site_name=tr.text('site_name')):errors.append(filename+': guide HTML title mismatch')
    # HTMLParser.text concatenates adjacent block text without separators.
    # Preserve visible word boundaries, including character references.
    visible=re.sub(r'<(script|style)\b[^>]*>.*?</\1>',' ',raw,flags=re.S|re.I)
    visible=unescape(re.sub(r'<[^>]+>',' ',visible))
    if re.search(r'\bAI\b',visible):errors.append(filename+': forbidden owner wording')
    from .audience_audit import Tags
    tags=Tags(raw).tags
    if any('hidden' in a or 'style' in a or a.get('aria-hidden')=='true' or any(k.lower().startswith('on') for k in a) for tag,a in tags):
        errors.append(filename+': hidden or executable guide content')
    if any(tag=='script' and (a.get('type')!='application/ld+json' or 'src' in a) for tag,a in tags) or page.metadata.get('refresh'):
        errors.append(filename+': executable or redirecting guide')
    css=Path(__file__).with_name('design.css').read_text(encoding='utf-8')
    css+='\n[data-growth="embed"] textarea{display:block;width:100%;max-width:100%;box-sizing:border-box;resize:vertical;font:13px/1.5 ui-monospace,monospace;padding:10px;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:8px}\n'
    if [n[2] for n in page.growth_nodes if n[0]=='style']!=[css] or any(tag=='link' and a.get('rel')=='stylesheet' for tag,a in tags):
        errors.append(filename+': unaudited guide visibility stylesheet')
    if len(re.findall(r'<article data-guide="'+key+'">',raw))!=1:errors.append(filename+': missing visible guide article')
    hrefs=[a.get('href') for tag,a in page.links if tag=='a']
    expected_links={tr.language.path('/guides/'+s+'/') for s in GUIDES if s!=slug}
    expected_links.update(tr.language.path('/compare/'+s+'/') for s in PAIRS)
    expected_links.add(tr.language.path('/methodology'))
    if include_workloads and slug=='choose-gpu-cloud-llm-inference':
        from .workload_specs import WORKLOADS, route
        expected_links.update(tr.language.path(route(s)) for s in WORKLOADS)
        related=re.findall(r'<h2 data-search="guide:related">.*?</h2><ul data-workload-related="true">(.*?)</ul>',raw,re.S)
        related_links=[tr.language.path('/guides/'+s+'/') for s in GUIDES if s!=slug]+[tr.language.path(route(s)) for s in WORKLOADS]
        if len(related)!=1 or [a.get('href') for tag,a in Tags(related[0]).tags if tag=='a']!=related_links:
            errors.append(filename+': workload links missing from guide Related list')
    source_urls=[SOURCES[s][1] for s in GUIDES[slug][1]] if slug else []
    expected_links.update(source_urls)
    unique={p:r for p,r in selected}
    cards=sorted(unique.items(),key=lambda e:(_rate(e[1]),e[0]))
    for p,r in cards:expected_links.add(tr.language.path('/'+slugs[r['gpu_model']]+'/'))
    if not expected_links<=set(hrefs):errors.append(filename+': missing guide source or internal link')
    _audit_cards(page,'guide',tr,facts,filename,errors,entries_override=cards)
    articles=[d for d in page.structured if d.get('@type')=='Article']
    expected_article={'@context':'https://schema.org','@type':'Article','headline':title,'description':description,
        'inLanguage':tr.language.code,'url':origin+tr.language.path('/guides/'+slug+'/'),
        'mainEntityOfPage':origin+tr.language.path('/guides/'+slug+'/'),
        **({'dateModified':data['updated']} if data['updated'] else {}),'citation':source_urls}
    if articles!=([expected_article] if slug else []):errors.append(filename+': guide Article differs from visible content')
    expected_rows=[];table_values=[]
    def m(section,i,field,value,fmt='number'):
        text=format(value,'.6f') if fmt=='precise' else format(value,'.2f')+'%' if fmt=='percent' else _dollars(value) if fmt=='money' else format(value,'g')
        table_values.append(('/guides/'+section+'/'+str(i)+'/'+field,fmt,text))
        return text
    def quote(entry):
        if not entry:return tr.text('not_observed')
        p,r=entry[:2]
        table_values.append((p+'/price_per_gpu_hour','money',_dollars(_rate(r))))
        return r['gpu_model']+' · '+r['provider']+' · '+_dollars(_rate(r))+' · '+tr.text(r['price_type'])
    if key=='choose':
        for i,w in enumerate(data['weights']):expected_rows.append([w['model']]+[m('weights',i,f,w[f]) for f in ('parameters','fp16','int4')])
        for i,c in enumerate(data['classes']):
            label=m('classes',i,'lower',c['lower'])+' GB'+(' – < '+m('classes',i,'upper',c['upper'])+' GB' if c['upper'] else '+')
            expected_rows.append([label,tr.text(c['kind']),quote(c['quote'])])
    elif key=='spot':
        for i,d in enumerate(data['discounts']):expected_rows.append([d['model']]+[m('discounts',i,f,d[f],'percent' if f=='percent' else 'money' if f in {'demand','spot'} else 'number') for f in ('count','demand','spot','percent')])
    elif key=='value':
        for i,d in enumerate(data['values']):expected_rows.append([d['model'],tr.text(d['kind']),m('values',i,'vram',d['vram']),m('values',i,'ratio',d['ratio'],'precise'),quote(d['quote'])])
    if page.table_rows!=expected_rows:errors.append(filename+': guide table selection/order/units differ from SQL')
    header_keys={
        'choose':['gpu_model','guide_params','guide_fp16','guide_int4','guide_vram_class','price_type','guide_quote'],
        'spot':['gpu_model','guide_pairs','guide_demand_median','guide_spot_median','guide_discount'],
        'value':['gpu_model','price_type','vram','guide_ratio','guide_quote'],
    }.get(key,[])
    caption_keys={'choose':['guide_weights_caption','guide_classes_caption'],
                  'spot':['guide_discount_caption'],'value':['guide_value_caption']}.get(key,[])
    content=lambda tag:[unescape(re.sub(r'<[^>]+>','',s)) for s in re.findall(r'<'+tag+r'\b[^>]*>(.*?)</'+tag+'>',raw,re.S)]
    if content('th')!=[tr.text(k) for k in header_keys] or content('caption')!=[tr.text(k) for k in caption_keys]:
        errors.append(filename+': guide column units or captions mismatch')
    outside=[v for v in page.outside_values if v[0] not in {'/guides/updated','/page_updated','/built'}]
    if outside!=table_values:errors.append(filename+': missing/changed guide numeric pointers')
    for p,fmt,text in page.values:
        try:
            v=_resolve({**facts,'guides':data,'page_updated':data['updated']},p)
            if fmt=='precise':expected=format(v,'.6f')
            elif fmt=='percent':expected=format(v,'.2f')+'%'
            elif fmt=='money':expected=_dollars(v)
            elif fmt=='number':expected=format(v,'g')
            elif fmt=='date':expected=v[:10]
            elif fmt=='clock':expected=datetime.fromisoformat(v.replace('Z','+00:00')).strftime('%H:%M UTC')
            elif fmt.startswith('instance-'):expected=_dollars(v['price_per_hour']*(v['gpu_count'] if v['price_scope']=='per_gpu' else 1))
            elif fmt.startswith('minimum-charge-'):expected=_dollars(v['price_per_hour']*(v['gpu_count'] if v['price_scope']=='per_gpu' else 1)*v['minimum_billing_seconds']/3600)
            else:expected=str(v)
            if text!=expected:errors.append(filename+': guide statistic differs from SQL '+p)
        except (KeyError,IndexError,ValueError,TypeError):errors.append(filename+': invalid guide statistic '+p)
    required=[('/guides/updated','date',data['updated'][:10]),('/page_updated','date',data['updated'][:10])] if data['updated'] else []
    required.append(('/built','date',facts['built'][:10]))
    if any(v not in page.values for v in required):errors.append(filename+': missing guide observation/build date')
    permitted=_allowed_literals(tr,facts)|{w['model'] for w in data['weights']}
    permitted.update(tr.text('compare_label',left=a,right=b) for a,b in PAIRS.values())
    for text in page.unmarked:
        if text not in permitted:errors.append(filename+': unaudited guide numeric text '+repr(text))
    return expected_search,title,description
