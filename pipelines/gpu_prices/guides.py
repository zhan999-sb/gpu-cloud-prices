"""Small evergreen guides built from the same current observation snapshot."""
from html import escape
from decimal import Decimal
import re
from statistics import median
from types import SimpleNamespace

from .guide_specs import GUIDES, SOURCES
from .models import popular_rank, POPULAR
from .regions import eligible
from .search_specs import PAIRS


def snapshot(model_data, references):
    entries = []
    for model, data in model_data.items():
        for i, row in enumerate(data['offers']):
            if eligible(SimpleNamespace(**row)):
                path = '/models/' + model.replace('~', '~0').replace('/', '~1') + '/offers/' + str(i)
                entries.append((path, row))
    rank = lambda e: (e[1]['price_per_gpu_hour'], e[0])
    known = []
    for path, row in entries:
        # Only explicit decimal GB in the canonical DB read view. Do not guess
        # capacity from family names, pooled instance memory or host free RAM.
        match = re.search(r'\b(\d+(?:\.\d+)?)GB$', row['gpu_model'])
        if match and float(match[1]) > 0:
            known.append((path, row, float(match[1])))
    classes = []
    for lower, upper in ((16, 24), (24, 48), (48, 80), (80, None)):
        local = [(p, r, v) for p, r, v in known if lower <= v and (upper is None or v < upper)]
        for kind in ('on_demand', 'spot'):
            choices = [e for e in local if e[1]['price_type'] == kind]
            best = min(choices, key=rank) if choices else None
            classes.append(dict(lower=lower, upper=upper, kind=kind, quote=best))
    groups = {}
    for path, row in entries:
        key = tuple(row[k] for k in ('gpu_model', 'provider', 'sku', 'region', 'gpu_count',
                    'gpu_fraction', 'price_scope', 'currency', 'tax_basis', 'vcpu', 'ram_gb', 'minimum_billing_seconds'))
        types = groups.setdefault(key, {})
        old = types.get(row['price_type'])
        if old is None or rank((path, row)) < rank(old):
            types[row['price_type']] = (path, row)
    pairs = {}
    for types in groups.values():
        if set(types) != {'on_demand', 'spot'}:
            continue
        demand, spot = types['on_demand'], types['spot']
        model = demand[1]['gpu_model']
        if popular_rank(model) < len(POPULAR):
            pairs.setdefault(model, []).append((demand, spot))
    discounts = []
    for model in sorted(pairs, key=lambda m: (popular_rank(m), m)):
        local = pairs[model]
        discounts.append(dict(model=model, count=len(local),
            demand=median(d[1]['price_per_gpu_hour'] for d, s in local),
            spot=median(s[1]['price_per_gpu_hour'] for d, s in local),
            # Matched counts/scopes cancel. Use source amounts as decimal
            # inputs so a half-cent percentage is not perturbed by binary
            # normalization before the final display rounding.
            percent=median(Decimal(100) * (1 - Decimal(str(s[1]['price_per_hour'])) / Decimal(str(d[1]['price_per_hour']))) for d, s in local),
            pairs=local))
    # Prefer recognizable multi-GPU examples; regional duplicates of the same
    # model do not displace another model's billing explanation.
    multi = sorted((e for e in entries if e[1]['gpu_count'] > 1), key=lambda e:(popular_rank(e[1]['gpu_model']),*rank(e)))
    billing, seen = [], set()
    for entry in multi:
        if entry[1]['gpu_model'] not in seen:
            billing.append(entry)
            seen.add(entry[1]['gpu_model'])
        if len(billing)==3:break
    if len(billing) < 3:
        billing += [e for e in sorted(entries, key=rank) if e not in billing][:3-len(billing)]
    values = []
    for model in sorted({r['gpu_model'] for p, r, v in known}, key=lambda m: (popular_rank(m), m)):
        for kind in ('on_demand', 'spot'):
            local = [e for e in known if e[1]['gpu_model'] == model and e[1]['price_type'] == kind]
            if local:
                p, r, v = min(local, key=rank)
                values.append(dict(model=model, kind=kind, vram=v, ratio=r['price_per_gpu_hour']/v, quote=(p,r)))
    weights = [dict(model=name, parameters=size, fp16=size*2, int4=size/2, source=url)
               for name, size, url in references]
    return dict(entries=entries, classes=classes, discounts=discounts, billing=billing, values=values, weights=weights,
                counts=dict(index=len(entries), choose=len(known) if weights else 0,
                            spot=sum(d['count'] for d in discounts), billing=len(entries),
                            glossary=len(entries), value=len(known)),
                updated=max((r['collected_at'] for _,r in entries), default=None))


def render(slug, data, slugs, tr, *, stat, table, card):
    """Renderer helpers are injected to avoid a circular import."""
    key = GUIDES[slug][0] if slug else 'index'
    route = '/guides/' + (slug+'/' if slug else '')
    title = tr.text('guide_'+key+'_title')
    description = tr.text('guide_'+key+'_intro')
    body = '<article data-guide="'+key+'"><h1>'+escape(title)+'</h1>'
    texts = {}
    def prose(name, tag='p'):
        nonlocal body
        marker = 'guide:'+name
        text = tr.text('guide_'+name)
        texts[marker] = text
        body += '<'+tag+' data-search="'+marker+'">'+escape(text)+'</'+tag+'>'
    def metric(section, i, field, value, fmt='number'):
        return stat('/guides/'+section+'/'+str(i)+'/'+field, value, fmt)
    def quote_cell(entry):
        if not entry:
            return tr.html('not_observed')
        p,r=entry[:2]
        return ('<a href="'+tr.language.path('/'+slugs[r['gpu_model']]+'/')+'">'+escape(r['gpu_model'])+'</a> · '
                +'<a href="'+tr.language.path('/providers/'+('clore-ai' if r['provider']=='clore' else r['provider'].replace('_','-'))+'/')+'">'
                +escape(r['provider'])+'</a> · '+stat(p+'/price_per_gpu_hour',r['price_per_gpu_hour'],'money')+' · '+tr.html(r['price_type']))
    def rows(headers, content, caption):
        nonlocal body
        body += table([tr.text(h) for h in headers],
                      ['<tr>'+''.join('<td>'+c+'</td>' for c in row)+'</tr>' for row in content], tr.text(caption))
    prose(key+'_intro')
    prose('basis')
    if data['updated']:
        body += '<p>'+tr.html('guide_observed')+': '+stat('/guides/updated',data['updated'],'date')+'</p>'
    if key=='index':
        body += '<ul>'+''.join('<li><a href="'+tr.language.path('/guides/'+s+'/')+'">'+tr.html('guide_'+k+'_title')+'</a><p>'+tr.html('guide_'+k+'_intro')+'</p></li>' for s,(k,_) in GUIDES.items())+'</ul>'
    elif key=='choose':
        prose('memory')
        rows(['gpu_model','guide_params','guide_fp16','guide_int4'],
             [[escape(w['model'])]+[metric('weights',i,f,w[f]) for f in ('parameters','fp16','int4')] for i,w in enumerate(data['weights'])], 'guide_weights_caption')
        prose('choose_steps')
        rows(['guide_vram_class','price_type','guide_quote'],
             [[metric('classes',i,'lower',c['lower'])+' GB'+(' – &lt; '+metric('classes',i,'upper',c['upper'])+' GB' if c['upper'] else '+'),
               tr.html(c['kind']),quote_cell(c['quote'])] for i,c in enumerate(data['classes'])], 'guide_classes_caption')
    elif key=='spot':
        prose('spot_method');prose('interruptions')
        rows(['gpu_model','guide_pairs','guide_demand_median','guide_spot_median','guide_discount'],
             [[escape(d['model'])]+[metric('discounts',i,f,d[f],'percent' if f=='percent' else 'money' if f in {'demand','spot'} else 'number')
               for f in ('count','demand','spot','percent')] for i,d in enumerate(data['discounts'])], 'guide_discount_caption')
        if not data['discounts']:prose('missing_pairs')
    elif key=='billing':
        prose('billing_math');prose('billing_limits')
    elif key=='glossary':
        for name in ('term_demand','term_spot','term_reserved','term_market','term_tax','term_from','term_component','term_regions'):
            prose(name)
    elif key=='value':
        prose('value_method')
        rows(['gpu_model','price_type','vram','guide_ratio','guide_quote'],
             [[escape(d['model']),tr.html(d['kind']),metric('values',i,'vram',d['vram']),metric('values',i,'ratio',d['ratio'],'precise'),quote_cell(d['quote'])]
              for i,d in enumerate(data['values'])], 'guide_value_caption')
    if key!='index':
        selected = (data['billing'] if key=='billing' else
                    [c['quote'][:2] for c in data['classes'] if c['quote']] if key=='choose' else
                    [e for d in data['discounts'] for pair in d['pairs'] for e in pair][:6] if key=='spot' else
                    [d['quote'] for d in data['values']][:3] if key=='value' else
                    sorted(data['entries'],key=lambda e:(e[1]['price_per_gpu_hour'],e[0]))[:1])
        # Show the complete audited offer card behind every selected table quote.
        if key=='value': selected=[d['quote'] for d in data['values']]
        unique={p:r for p,r in selected}
        prose('examples','h2')
        for p,r in sorted(unique.items(),key=lambda e:(e[1]['price_per_gpu_hour'],e[0])):
            body += card(r,p,tr,model_url=tr.language.path('/'+slugs[r['gpu_model']]+'/'))
        prose('sources','h2')
        body += '<ul data-search="guide:source-list">'+''.join('<li><a href="'+escape(SOURCES[s][1],quote=True)+'">'+escape(SOURCES[s][0])+'</a></li>' for s in GUIDES[slug][1])+'</ul>'
    if data['counts'][key]<3:prose('thin')
    prose('related','h2')
    body += '<ul>'+''.join('<li><a href="'+tr.language.path('/guides/'+s+'/')+'">'+tr.html('guide_'+k+'_title')+'</a></li>' for s,(k,_) in GUIDES.items() if s!=slug)+'</ul>'
    body += '<p><a href="'+tr.language.path('/methodology')+'">'+tr.html('methodology')+'</a></p><ul>'
    body += ''.join('<li><a href="'+tr.language.path('/compare/'+s+'/')+'">'+tr.html('compare_label',left=a,right=b)+'</a></li>' for s,(a,b) in PAIRS.items())+'</ul></article>'
    return title, description, body, data['counts'][key]


def article(slug, title, description, updated, tr, origin):
    return {'@context':'https://schema.org','@type':'Article','headline':title,'description':description,
            'inLanguage':tr.language.code,'url':origin+tr.language.path('/guides/'+slug+'/'),
            'mainEntityOfPage':origin+tr.language.path('/guides/'+slug+'/'),
            **({'dateModified':updated} if updated else {}),
            'citation':[SOURCES[s][1] for s in GUIDES[slug][1]]}
