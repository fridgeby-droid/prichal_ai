from app.services.safe_errors import error_details


def test_lock_reason_is_visible():
    result=error_details(ValueError('BONUS_LOADER_ALREADY_RUNNING'))
    assert result['error_code']=='BONUS_LOADER_ALREADY_RUNNING' and result['next']


def test_arbitrary_exception_text_not_exposed():
    for exc in (ValueError('token=secret'),RuntimeError('https://secret.example')):
        result=error_details(exc)
        assert result['error_code']=='UNCLASSIFIED_ERROR'
        assert 'secret' not in str(result)
