from scripts.diagnose_sale import financial_details


def test_discount_not_automatically_bonus_and_missing_not_zero():
    raw={'TotalPrice':1750.15,'TotalDiscount':36,'CustomerName':'Private Name','Customer':123,
         'Payments':[{'Amount':1750.15,'BankSum':1750.15,'PayBank':1750.15,'Comment':'private'}],
         'DiscountInfo':[{'Name':'Скидка','Value':36,'PercentValue':2}]}
    result=financial_details(raw)
    assert result['loyalty_status']=='UNCONFIRMED'
    assert result['sale']['TotalPrice']=='1750.15'
    assert result['discounts'][0]['value']=='36'
    assert result['payments'][0]['CashSum'] is None
    assert 'Private Name' not in str(result) and 'private' not in str(result)


def test_empty_discounts_do_not_infer_accrual():
    result=financial_details({'TotalPrice':2171.70,'DiscountInfo':[]})
    assert result['discounts']==[] and result['payments'] is None
    assert result['loyalty_status']=='UNCONFIRMED'
    assert '108' not in str(result)


def test_redact_potential_contacts_and_non_numeric_payload():
    result=financial_details({'DiscountInfo':[{'Name':'Карта 123456789012 foo@example.com','Value':'secret','PercentValue':True}]})
    row=result['discounts'][0]
    assert '123456789012' not in row['name'] and 'example.com' not in row['name']
    assert row['value'] is None and row['percent_value'] is None
