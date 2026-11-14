from app.services import product_costs,stock_history

KINDS={'product_costs','stock','stockouts'}


async def report(kind,start,end,point_id='',offset=0,export_all=False):
    if kind=='product_costs':
        return await product_costs.configured_report(start,end,point_id,offset,export_all)
    if kind in ('stock','stockouts'):
        return await stock_history.report(start,end,point_id,kind,offset,export_all)
    raise ValueError('Виды: product_costs, stock, stockouts')
