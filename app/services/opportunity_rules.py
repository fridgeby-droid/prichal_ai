"""Explicit stable-ID rules. Names are for selection, never the matching key."""
import json
import re
from app.db.database import pool
from app.services.personal_followups import require_actor
KEY='analytics:opportunity-rules:v1'


async def search(query):
    if not query.strip() or len(query)>120: raise ValueError('Введите название товара, до 120 символов.')
    async with pool().acquire() as conn:
        rows=await conn.fetch('''SELECT CASE WHEN NULLIF(product_uuid,'') IS NOT NULL THEN 'uuid:'||product_uuid
            ELSE 'id:'||product_id::text END product_key,MAX(name) name,unit_name unit
            FROM retail_sale_items WHERE NOT refused AND (NULLIF(product_uuid,'') IS NOT NULL OR product_id IS NOT NULL)
            AND POSITION(LOWER($1) IN LOWER(name))>0 GROUP BY product_key,unit_name ORDER BY name LIMIT 31''',query.strip())
        recent=await conn.fetch('SELECT raw_json FROM retail_sales WHERE NOT deleted AND NOT is_return ORDER BY business_date DESC,sale_id DESC LIMIT 200')
    from app.services.sale_tree import nodes
    candidates={(r['product_key'],r['unit']):dict(r) for r in rows}
    for sale in recent:
        for node in nodes(sale['raw_json']):
            i=node['item'];name=str(i.get('Name') or '')
            if i.get('IsModifier') is not True or node['refused'] or node['is_return'] or query.casefold() not in name.casefold(): continue
            product='uuid:'+str(i['NomenclatureUUID']) if i.get('NomenclatureUUID') else 'id:'+str(i['Nomenclature']) if i.get('Nomenclature') else None
            if product: candidates[(product,str(i.get('UnitName') or ''))]={'product_key':product,'name':name,'unit':str(i.get('UnitName') or ''),'source':'recent_modifier_sample'}
    result=list(candidates.values())
    return {'rows':result[:30],'truncated':len(result)>30,'modifier_search':'Последние 200 сохранённых продаж; отсутствие кандидата не доказывает отсутствие модификатора.'}


def validated(kind,name,base,target):
    if kind not in ('crosssell','substitution'): raise ValueError('Вид: crosssell или substitution')
    if not name.strip() or len(name)>60: raise ValueError('Название правила: 1–60 символов')
    a=sorted(set(base));b=sorted(set(target))
    if not a or not b or len(a)>50 or len(b)>50 or set(a)&set(b): raise ValueError('Нужны непересекающиеся непустые группы до 50 ID.')
    if any(not re.fullmatch(r'(id:[1-9][0-9]*|uuid:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})',v) for v in a+b): raise ValueError('Используйте стабильные id:/uuid: из поиска товаров.')
    return {'id':name.strip(),'base':a,'target':b}


async def load():
    from app.services.business_insights import rules
    defaults=rules()
    async with pool().acquire() as conn: saved=await conn.fetchval('SELECT value FROM app_meta WHERE key=$1',KEY)
    saved=json.loads(saved) if isinstance(saved,str) else saved
    if not saved: return defaults
    return {kind:list({r['id']:r for r in defaults.get(kind,[])+saved.get(kind,[])}.values()) for kind in ('crosssell','substitution')}


async def save(actor,kind,name,base,target):
    require_actor(actor);rule=validated(kind,name,base,target)
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute('INSERT INTO app_meta(key,value,updated_at) VALUES($1,$2::jsonb,now()) ON CONFLICT(key) DO NOTHING',KEY,'{}')
            value=await conn.fetchval('SELECT value FROM app_meta WHERE key=$1 FOR UPDATE',KEY)
            data=json.loads(value) if isinstance(value,str) else value
            current={r['id']:r for r in data.get(kind,[])}
            if rule['id'] not in current and len(current)>=50: raise ValueError('Не более 50 правил каждого вида.')
            current[rule['id']]=rule;data[kind]=list(current.values())
            await conn.execute('UPDATE app_meta SET value=$2::jsonb,updated_at=now() WHERE key=$1',KEY,json.dumps(data,ensure_ascii=False))
    return rule
