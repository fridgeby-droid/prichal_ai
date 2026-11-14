import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from app.services import voice as v


def setup():
    settings = NS(allowed_user_ids={1}, voice_enabled=True, voice_max_duration_seconds=120,
                  voice_transcription_model='gpt-transcribe', openai_api_key='test')
    status = NS(edit_text=AsyncMock())
    async def download(file_id, destination, timeout):
        destination.write(b'OggS-test')
        destination.seek(0)
    message = NS(from_user=NS(id=1), voice=NS(duration=30, file_size=9, file_id='file'),
                 bot=NS(download=AsyncMock(side_effect=download)), answer=AsyncMock(return_value=status))
    return settings, message, status, AsyncMock()


def test_voice_routes_exact_text_and_original_actor(monkeypatch):
    s, m, status, ask = setup()
    transcription = AsyncMock(return_value='Сравни дневную смену Батумской за октябрь и сентябрь')
    monkeypatch.setattr(v, 'transcribe', transcription)
    asyncio.run(v.handle_voice(m, s, ask))
    ask.assert_awaited_once_with(m, request_text=transcription.return_value)
    assert status.edit_text.call_args.kwargs['parse_mode'] is None
    assert not v._active_users
    assert transcription.call_args.args[0].closed


@pytest.mark.parametrize('case', ['unauthorized', 'disabled', 'long', 'large', 'busy'])
def test_reject_before_download(monkeypatch, case):
    s, m, status, ask = setup()
    if case == 'unauthorized': m.from_user.id = 99
    if case == 'disabled': s.voice_enabled = False
    if case == 'long': m.voice.duration = 121
    if case == 'large': m.voice.file_size = v.MAX_BYTES + 1
    if case == 'busy': v._active_users.add(1)
    try:
        asyncio.run(v.handle_voice(m, s, ask))
        m.bot.download.assert_not_awaited()
        ask.assert_not_awaited()
    finally:
        v._active_users.clear()


@pytest.mark.parametrize('error', [TimeoutError('secret'), RuntimeError('secret'), v.VoiceError('Не разобрано')])
def test_failure_no_agent_and_releases_capacity(monkeypatch, error):
    s, m, status, ask = setup()
    monkeypatch.setattr(v, 'transcribe', AsyncMock(side_effect=error))
    asyncio.run(v.handle_voice(m, s, ask))
    ask.assert_not_awaited()
    assert 'secret' not in str(status.edit_text.call_args)
    assert not v._active_users


def test_stream_limit_enforced_without_metadata(monkeypatch):
    monkeypatch.setattr(v, 'MAX_BYTES', 4)
    with v.LimitedBuffer() as buffer:
        buffer.write(b'1234')
        with pytest.raises(v.VoiceError): buffer.write(b'5')
        assert len(buffer.getvalue()) == 4


@pytest.mark.parametrize('text', [None, '', '   ', 'x' * 12001])
def test_invalid_transcript(monkeypatch, text):
    s, _, _, _ = setup()
    client = NS(audio=NS(transcriptions=NS(create=AsyncMock(return_value=NS(text=text)))))
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr(v, 'AsyncOpenAI', lambda **kwargs: context)
    with v.LimitedBuffer() as buffer:
        with pytest.raises(v.VoiceError): asyncio.run(v.transcribe(buffer, s))


def test_sdk_contract_and_cleanup(monkeypatch):
    s, _, _, _ = setup()
    create = AsyncMock(return_value=NS(text='  Батумская 5  '))
    context = AsyncMock()
    context.__aenter__.return_value = NS(audio=NS(transcriptions=NS(create=create)))
    monkeypatch.setattr(v, 'AsyncOpenAI', lambda **kwargs: context)
    with v.LimitedBuffer() as buffer:
        assert asyncio.run(v.transcribe(buffer, s)) == 'Батумская 5'
        assert create.call_args.kwargs == dict(model='gpt-transcribe', file=('voice.ogg', buffer, 'audio/ogg'))
    context.__aexit__.assert_awaited_once()
