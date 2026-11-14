"""Actual line costs from saved receipts; planned costs are never substituted."""
from collections import Counter
import json
import os
from decimal import Decimal as D
from app.db.database import pool
from app.services.sale_tree import nodes,numeric
from app.services.extra_reports import dates
from app.services.period_reports import point_filter


class Costs:
    def __init__(self,basis='unconfirmed'):
        if basis not in ('unconfirmed','same_as_total_price','revenue_excludes_vat'):
            raise ValueError('COST_BASIS_INVALID')
        self.basis=basis;self.groups={};self.counts=Counter()

    def add(self,sale):
        entries=nodes(sale['raw_json'])
        parents={n['parent_key'] for n in entries if n['parent_key']}
        for node in entries:
            i=node['item']
            if node['refused']: continue
            # Nested/kit totals may overlap parents; do not invent cost allocation.
            if node['parent_key'] or node['line_key'] in parents or i.get('IsModifier') is True or i.get('IsKit') is True or i.get('Folder@') is True:
                self.counts['excluded_complex_lines']+=1;continue
            pid=('uuid:'+str(i['NomenclatureUUID']) if i.get('NomenclatureUUID') else
                 'id:'+str(i['Nomenclature']) if i.get('Nomenclature') else None)
            if not pid: self.counts['unidentified_lines']+=1;continue
            unit=str(i.get('UnitName') or '')
            key=(str(sale['point_id']),pid,unit)
            row=self.groups.setdefault(key,{'point_id':key[0],'product_key':pid,'unit':unit,'name':str(i.get('Name') or pid),
                'sales_lines':0,'cost_known_lines':0,'cost_missing_lines':0,'zero_cost_lines':0,
                'known_revenue':D(0),'known_cost':D(0),'margin_covered_revenue':D(0),'margin_covered_cost':D(0),
                'margin_covered_lines':0,'return_lines':0,'known_return_revenue':D(0),'known_return_cost':D(0),
                'return_cost_missing_lines':0,'unknown_revenue_lines':0,'unknown_return_revenue_lines':0})
            returned=bool(sale.get('is_return')) or node['is_return']
            revenue=numeric(i.get('TotalPrice'));cost=numeric(i.get('TotalCost'))
            # Negative non-return values are anomalies, not free goods or negative cost.
            if revenue is not None and returned: revenue=abs(revenue)
            if cost is not None and returned: cost=abs(cost)
            if cost is not None and cost<0: cost=None
            if revenue is not None and revenue<0: revenue=None
            if returned:
                row['return_lines']+=1
                if revenue is not None: row['known_return_revenue']+=revenue
                else: row['unknown_return_revenue_lines']+=1
                if cost is not None: row['known_return_cost']+=cost
                else: row['return_cost_missing_lines']+=1
                continue
            row['sales_lines']+=1
            if revenue is None: row['unknown_revenue_lines']+=1
            else: row['known_revenue']+=revenue
            if cost is None: row['cost_missing_lines']+=1;continue
            row['cost_known_lines']+=1;row['known_cost']+=cost
            if cost==0:
                row['zero_cost_lines']+=1
                continue  # Cannot distinguish a free item from uncalculated source cost yet.
            if revenue is None or self.basis=='unconfirmed': continue
            if self.basis=='revenue_excludes_vat':
                vat=numeric(i.get('TotalVAT'))
                if vat is None or not 0<=vat<=revenue: continue
                revenue-=vat
            row['margin_covered_revenue']+=revenue;row['margin_covered_cost']+=cost
            row['margin_covered_lines']+=1

    def result(self):
        rows=[]
        for row in self.groups.values():
            r=dict(row);covered=r['margin_covered_lines']
            r['gross_margin']=r['margin_covered_revenue']-r['margin_covered_cost'] if covered else None
            r['gross_margin_percent']=(r['gross_margin']/r['margin_covered_revenue']*100
                if covered and r['margin_covered_revenue'] else None)
            r['cost_coverage_percent']=D(r['cost_known_lines'])*100/r['sales_lines'] if r['sales_lines'] else None
            r['cost_basis_confirmed']=self.basis!='unconfirmed'
            if not r['cost_known_lines']: r['known_cost']=None
            if r['sales_lines']==r['unknown_revenue_lines']: r['known_revenue']=None
            if r['return_lines']==r['return_cost_missing_lines']: r['known_return_cost']=None
            if r['return_lines']==r['unknown_return_revenue_lines']: r['known_return_revenue']=None
            rows.append(r)
        rows.sort(key=lambda r:(-(r['known_revenue'] or D(0)),r['point_id'],r['product_key'],r['unit']))
        return rows,{'basis':'actual_TotalCost_and_line_TotalPrice','tax_basis':self.basis,
            'exclusions':dict(self.counts),'planned_cost_used':False,'net_profit_calculated':False,
            'note':'Маржа только покрытых строк на согласованной базе. Нулевая стоимость требует проверки и в маржу не включена. Возвраты отдельно. Комплекты/вложенные строки исключены; чистая прибыль не рассчитывается.'}


async def report(start,end,point_id='',offset=0,export_all=False,basis='unconfirmed'):
    first,last=dates(start,end);point=await point_filter(point_id)
    if offset<0 or (last-first).days>=93: raise ValueError('Выберите до 93 дней, offset >= 0')
    calc=Costs(basis)
    async with pool().acquire() as conn:
        async with conn.transaction(isolation='repeatable_read',readonly=True):
            count=0
            async for sale in conn.cursor('''SELECT point_id,sale_id,is_return,raw_json FROM retail_sales
                WHERE NOT deleted AND business_date BETWEEN $1 AND $2
                AND point_id<>23109 AND ($3::bigint IS NULL OR point_id=$3)''',first,last,point,prefetch=50):
                count+=1
                if count>500000: raise ValueError('Сузьте период: больше 500000 продаж')
                calc.add(dict(sale))
    rows,meta=calc.result()
    return {'kind':'product_costs','point_id':str(point) if point else None,'date_from':start,'date_to':end,
        'rows':rows if export_all else rows[offset:offset+50],'total_rows':len(rows),
        'next_offset':offset+50 if not export_all and offset+50<len(rows) else None,
        'metadata':meta,'complete_source':False,'sales_read':count}


async def configured_report(start,end,point_id='',offset=0,export_all=False):
    # Explicit operator choice, never a language-model argument or guessed tax system.
    settings=json.loads(os.environ.get('COST_BASIS_BY_POINT','{}'))
    if not isinstance(settings,dict): raise ValueError('COST_BASIS_BY_POINT_INVALID')
    # Network cannot apply one store's basis to others; use unconfirmed diagnostics.
    basis=settings.get(str(point_id),'unconfirmed') if point_id else 'unconfirmed'
    return await report(start,end,point_id,offset,export_all,basis)
