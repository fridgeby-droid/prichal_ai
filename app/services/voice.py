"""Bounded Telegram voice download and file transcription; no audio persistence."""
import asyncio
import io
import logging

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)
MAX_BYTES = 10 * 1024 * 1024
_active_users: set[int] = set()


class VoiceError(ValueError):
    pass


class LimitedBuffer(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_BYTES:
            raise VoiceError('Голосовое слишком большое. Запишите короткое сообщение.')
        return super().write(data)


async def transcribe(buffer, settings):
    # No automatic paid retries; failures can be retried explicitly by the user.
    async with AsyncOpenAI(api_key=settings.openai_api_key, timeout=60, max_retries=0) as client:
        result = await client.audio.transcriptions.create(
            model=settings.voice_transcription_model,
            file=('voice.ogg', buffer, 'audio/ogg'),
        )
    text = getattr(result, 'text', None)
    if not isinstance(text, str) or not text.strip():
        raise VoiceError('Не удалось разобрать речь. Запишите ещё раз или отправьте текст.')
    text = text.strip()
    if len(text) > 12000:
        raise VoiceError('Распознанный запрос слишком длинный. Разделите его на несколько сообщений.')
    return text


async def handle_voice(message, settings, ask):
    # Defence in depth: never download or send unauthorised audio to OpenAI.
    if not message.from_user or message.from_user.id not in settings.allowed_user_ids:
        return
    if not settings.voice_enabled:
        await message.answer('Голосовые запросы отключены. Напишите запрос текстом.')
        return
    voice = message.voice
    if not voice or not 0 < voice.duration <= settings.voice_max_duration_seconds:
        await message.answer(f'Запишите голосовое до {settings.voice_max_duration_seconds} секунд.')
        return
    if voice.file_size and voice.file_size > MAX_BYTES:
        await message.answer('Голосовое слишком большое. Запишите короткое сообщение.')
        return
    user_id = message.from_user.id
    if user_id in _active_users or len(_active_users) >= 2:
        await message.answer('Уже обрабатываю голосовой запрос. Отправьте сообщение чуть позже.')
        return
    # No await between checking and reserving capacity; no unbounded download queue.
    _active_users.add(user_id)
    try:
        status = await message.answer('🎙 Распознаю голосовое…')
        try:
            async with asyncio.timeout(90):
                with LimitedBuffer() as buffer:
                    await message.bot.download(voice.file_id, destination=buffer, timeout=30)
                    if not buffer.getbuffer().nbytes:
                        raise VoiceError('Голосовое пустое. Запишите ещё раз.')
                    buffer.seek(0)
                    text = await transcribe(buffer, settings)
        except VoiceError as exc:
            await status.edit_text(str(exc), parse_mode=None)
            return
        except Exception as exc:
            # Avoid logging Telegram file URLs, tokens, audio or recognised text.
            logger.warning('Voice processing failed: %s', type(exc).__name__)
            await status.edit_text('Не удалось распознать голосовое. Повторите позже или напишите текстом.')
            return
        await status.edit_text('🎙 Ваш запрос:\n' + text[:3000], parse_mode=None)
        for offset in range(3000, len(text), 3000):
            await message.answer(text[offset:offset + 3000], parse_mode=None)
        await ask(message, request_text=text)
    finally:
        _active_users.discard(user_id)
