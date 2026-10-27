"""Walk observed SaleNomenclatures/Positions without inventing modifier flags."""
import json
from decimal import Decimal, InvalidOperation


def nodes(raw):
    raw=json.loads(raw) if isinstance(raw,str) else raw
    if not isinstance(raw,dict): raise ValueError('SALE_TREE_INVALID')
    found={}
    def walk(items,parent=None,path='SaleNomenclatures',depth=0,returned=False,refused=False):
        if items is None: return
        if not isinstance(items,list) or depth>20: raise ValueError('SALE_TREE_INVALID')
        for index,item in enumerate(items):
            if not isinstance(item,dict): raise ValueError('SALE_TREE_INVALID')
            location=f'{path}[{index}]'
            identity=item.get('SaleNomenclature')
            key='id:'+str(identity) if identity is not None else 'key:'+str(item['Key']) if item.get('Key') else 'path:'+location
            effective_return=returned or item.get('IsReturn') is True
            effective_refused=refused or item.get('Refused') is True
            signature=json.dumps({k:v for k,v in item.items() if k!='Positions'},sort_keys=True,ensure_ascii=False)
            entry={'item':item,'line_key':key,'parent_key':parent,'path':location,
                   'is_return':effective_return,'refused':effective_refused,'signature':signature}
            old=found.get(key)
            if old:
                if old['signature']!=signature or old['is_return']!=effective_return or old['refused']!=effective_refused:
                    raise ValueError('SALE_TREE_CONFLICT: repeated line has incompatible facts')
                if parent and old['parent_key'] and parent!=old['parent_key']:
                    raise ValueError('SALE_TREE_CONFLICT: multiple parents')
                if parent: old['parent_key']=parent
            else: found[key]=entry
            walk(item.get('Positions'),key,location+'.Positions',depth+1,effective_return,effective_refused)
    walk(raw.get('SaleNomenclatures'),returned=raw.get('Return') is True)
    return list(found.values())


def numeric(value):
    if value is None or isinstance(value,bool): return None
    try:
        result=Decimal(str(value))
        return result if result.is_finite() else None
    except (ValueError,InvalidOperation): return None


def modifiers(sales,query='',match='contains'):
    groups={}
    for sale in sales:
        for node in nodes(sale['raw_json']):
            item=node['item']
            if item.get('IsModifier') is not True or node['refused']: continue
            name=str(item.get('Name') or '')
            pid=str(item.get('Nomenclature') or '')
            uuid=str(item.get('NomenclatureUUID') or '')
            if query:
                matches = (query.casefold()==name.casefold() if match=='exact' else
                           query in (pid,uuid,'id:'+pid,'uuid:'+uuid) if match=='id' else
                           query.casefold() in name.casefold() or query in (pid,uuid))
                if not matches: continue
            quantity=numeric(item.get('Quantity'))
            if quantity is None: raise ValueError('MODIFIER_QUANTITY_UNKNOWN: нельзя считать отсутствующее количество нулём')
            product_key='uuid:'+uuid if uuid else 'id:'+pid if pid else f"unknown:{sale['point_id']}:{sale['sale_id']}:{node['line_key']}"
            unit=str(item.get('UnitName') or '')
            key=(sale['point_id'],sale['seller_id'],product_key,unit)
            row=groups.setdefault(key,dict(point_id=sale['point_id'],seller_id=sale['seller_id'],seller_name=sale['seller_name'],
                product_key=product_key,name=name,unit=unit,quantity=Decimal(0),return_quantity=Decimal(0),
                revenue=Decimal(0),return_revenue=Decimal(0),unknown_return_lines=0,unknown_amount_lines=0,purchases=set()))
            if node['is_return']:
                row['return_quantity']+=abs(quantity)
                amount=numeric(item.get('TotalPrice'))
                if amount is None: row['unknown_return_lines']+=1
                else: row['return_revenue']+=abs(amount)
            else:
                row['quantity']+=quantity
                row['purchases'].add((sale['point_id'],sale['sale_id']))
                amount=numeric(item.get('TotalPrice'))
                if amount is None: row['unknown_amount_lines']+=1
                else: row['revenue']+=amount
    for row in groups.values():
        row['purchases']=len(row['purchases'])
        if row['unknown_amount_lines']: row['revenue']=None
        if row['unknown_return_lines']: row['return_revenue']=None
    return list(groups.values())
