import pytest
from app.services.store_search import search

ROWS=[{'point_id':5635,'name':'Батумская 5','address':None},
      {'point_id':2,'name':'Батумская 55','address':None},
      {'point_id':3,'name':'Камская 2/1','address':'ул. Камская, 2/1'},
      {'point_id':4,'name':'Вольская 62','address':None}]


@pytest.mark.parametrize('query',['Батумской 5','Батумскую 5','на Батумской, 5','Батумская 5','5635'])
def test_declined_store_and_exact_house(query):
    rows,_=search(ROWS,query)
    assert [r['point_id'] for r in rows]==[5635]


def test_ambiguous_name_returns_all_candidates():
    rows,_=search(ROWS,'Батумской')
    assert len(rows)==2
    assert len(search(ROWS,'Батум')[0])==2


def test_unknown_house_and_unrelated_street_not_selected():
    assert search(ROWS,'Батумской 6')[0]==[]
    assert search(ROWS,'Московской 5')[0]==[]
    assert [r['point_id'] for r in search(ROWS,'Камской 2/1')[0]]==[3]
    assert search(ROWS,'Камской 2')[0]==[]
