"""Conservative address matching; never choose one of multiple candidates."""
import re

STOP={'ул','улица','улице','улицы','на','в','магазин','магазина','магазине'}
ENDINGS=('ского','скому','скими','ская','ской','скую','ское','ские','ский','ским','ском',
         'цкая','цкой','цкую','цкое','цкие','цкий')


def tokens(value,inflections=False):
    parts=re.findall(r'\d+(?:/\d+)?[а-яa-z]?|[а-яa-z]+',(value or '').casefold().replace('ё','е'))
    result=[]
    for word in parts:
        if word in STOP: continue
        if inflections:
            for suffix in ENDINGS:
                if word.endswith(suffix) and len(word)>len(suffix)+2:
                    # Keep ск/цк to avoid matching unrelated short roots.
                    word=word[:-len(suffix)]+('ск' if suffix.startswith('ск') else 'цк')
                    break
        result.append(word)
    return set(result)


def search(rows,query):
    q=query.strip()
    if not q: return rows,'all'
    exact=[r for r in rows if str(r['point_id'])==q]
    if exact: return exact,'id'
    for inflections,mode in ((False,'tokens'),(True,'inflection')):
        wanted=tokens(q,inflections)
        if not wanted: return [],'none'
        matches=[r for r in rows if any(wanted<=tokens(r.get(field,''),inflections) for field in ('name','address'))]
        if matches: return matches,mode
    wanted=tokens(q)
    def partial(value):
        available=tokens(value)
        return all(word in available or (word.isalpha() and len(word)>=3 and
                   any(item.startswith(word) for item in available)) for word in wanted)
    matches=[r for r in rows if any(partial(r.get(field,'')) for field in ('name','address'))]
    if matches: return matches,'prefix'
    return [],'none'
