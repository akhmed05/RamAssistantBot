"""Offline regression tests: no polling, credentials or external requests."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

APP = Path(__file__).resolve().parents[1] / 'RamAssistantBot'
sys.path.insert(0, str(APP))

import httpx
from aiogram import types
from openai import AsyncOpenAI
import config
import handlers
import main

DUMMY_TOKEN = '123456789:' + 'A' * 35
DUMMY_KEY = 'offline-test-key'


def fake_message(text='Вопрос', prompt=None, bot=True):
    reply = None if prompt is None else SimpleNamespace(
        text=prompt, from_user=SimpleNamespace(is_bot=bot, id=123456789),
    )
    return SimpleNamespace(
        message_id=1, chat=SimpleNamespace(id=1, type="private"),
        from_user=SimpleNamespace(id=1), text=text, reply_to_message=reply, answer=AsyncMock(),
        reply=AsyncMock(), reply_document=AsyncMock(),
    )


class NoNetwork:
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = main.Store(Path(directory.name) / "ram.sqlite3")
        config_patch = patch.dict(os.environ, {"RAM_OWNER_IDS": "1"})
        config_patch.start()
        self.addCleanup(config_patch.stop)
        store_patch = patch.object(main, "_store", self.store)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        model_patch = patch.object(main, "OPENAI_MODEL", "offline-model")
        model_patch.start()
        self.addCleanup(model_patch.stop)
        for target in ('socket.socket.connect', 'socket.getaddrinfo'):
            guard = patch(target, side_effect=AssertionError('Network forbidden in tests'))
            guard.start()
            self.addCleanup(guard.stop)


class ConfigurationTests(NoNetwork, unittest.TestCase):
    def test_import_without_token_creates_no_bot_or_polling(self):
        script = """
import os, sys
from unittest.mock import patch
os.environ.pop('BOT_TOKEN', None)
sys.path.insert(0, sys.argv[1])
with patch('aiogram.Bot', side_effect=AssertionError('Bot created')), \
     patch('aiogram.executor.start_polling', side_effect=AssertionError('Polling started')):
    import main
"""
        result = subprocess.run(
            [sys.executable, '-c', script, str(APP)], capture_output=True, text=True,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_environment_overrides_and_key_alias(self):
        self.addCleanup(importlib.reload, config)
        with patch.dict(os.environ, {'RAM_OPENAI_API_KEY': DUMMY_KEY,
                                     'GOOGLE_SCRIPT_URL': 'https://example.test/form',
                                     'BOT_PROXY': 'http://example.test:8080'}, clear=True):
            importlib.reload(config)
            self.assertEqual(config.OPENAI_API_KEY, DUMMY_KEY)
            self.assertEqual(config.GOOGLE_SCRIPT_URL, 'https://example.test/form')
            self.assertEqual(config.BOT_PROXY, 'http://example.test:8080')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'preferred-test-key',
                                     'RAM_OPENAI_API_KEY': DUMMY_KEY}, clear=True):
            importlib.reload(config)
            self.assertEqual(config.OPENAI_API_KEY, 'preferred-test-key')

    def test_missing_bot_token_has_actionable_error(self):
        with patch.object(main, 'BOT_TOKEN', None):
            with self.assertRaisesRegex(ValueError, 'BOT_TOKEN'):
                main.create_dispatcher()


class FormAndUrlTests(NoNetwork, unittest.TestCase):
    def test_supported_youtube_links_keep_placeholder(self):
        for url in ('https://youtu.be/123', 'https://youtube.com/watch?v=123',
                    'http://www.youtube.com/watch?v=123', 'https://m.youtube.com/watch?v=1'):
            with self.subTest(url=url):
                self.assertTrue(handlers.is_youtube_url(url))
                self.assertIn(url, handlers.handle_youtube_summary(url))
                self.assertIn('в реальности', handlers.handle_youtube_summary(url))

    def test_invalid_and_lookalike_urls_are_rejected(self):
        for url in (None, '', 'not a url', 'https://youtube.com.evil.test/watch',
                    'https://evil.test/?q=youtu.be', 'https://youtube.com@evil.test/x',
                    'ftp://youtu.be/x', 'https://[invalid'):
            with self.subTest(url=url):
                self.assertFalse(handlers.is_youtube_url(url))
                self.assertIsNone(handlers.handle_youtube_summary(url))

    def test_form_payload_timeout_and_slashes_in_comment(self):
        with patch.object(handlers.requests, 'post', return_value=Mock(status_code=200)) as post:
            self.assertTrue(handlers.handle_google_form_submission(' Имя / 123 / текст / ещё '))
        post.assert_called_once_with(
            handlers.GOOGLE_SCRIPT_URL,
            data={'Имя': 'Имя', 'Телефон': '123', 'Комментарий': 'текст / ещё'}, timeout=15,
        )

    def test_malformed_forms_never_send_requests(self):
        with patch.object(handlers.requests, 'post') as post:
            for data in (None, '', 'Имя', 'Имя / 123', ' / 123 / текст',
                         'Имя / / текст', 'Имя / 123 / '):
                with self.subTest(data=data):
                    self.assertFalse(handlers.handle_google_form_submission(data))
            post.assert_not_called()

    def test_form_http_failure_and_timeout(self):
        with patch.object(handlers.requests, 'post', return_value=Mock(status_code=500)):
            self.assertFalse(handlers.handle_google_form_submission('Имя / 123 / текст'))
        for exc in (handlers.requests.Timeout(), handlers.requests.ConnectionError()):
            with self.subTest(exc=type(exc).__name__):
                with patch.object(handlers.requests, 'post', side_effect=exc):
                    self.assertFalse(handlers.handle_google_form_submission('Имя / 123 / текст'))


class HandlerTests(NoNetwork, unittest.IsolatedAsyncioTestCase):
    async def test_welcome_keeps_all_four_menu_items(self):
        message = fake_message()
        await main.send_welcome(message)
        keyboard = message.answer.call_args.kwargs['reply_markup'].to_python()['keyboard']
        self.assertEqual([button['text'] for row in keyboard for button in row], [
            '📹 YouTube-саммари', '💬 Задать вопрос AI', '📊 Отправить заявку', '📎 Получить PDF', '🤖 RAM AI',
        ])

    async def test_prompts_request_a_reply(self):
        for handler in (main.youtube_summary_prompt, main.ask_ai_prompt, main.google_form_prompt):
            with self.subTest(handler=handler.__name__):
                message = fake_message()
                await handler(message)
                markup = message.reply.call_args.kwargs['reply_markup']
                self.assertTrue(markup.force_reply)

    async def test_reply_filter_handles_nontext_and_unrelated_replies(self):
        self.assertTrue(main.is_prompt_reply(fake_message(prompt=main.AI_PROMPT), main.AI_PROMPT))
        self.assertFalse(main.is_prompt_reply(fake_message(text=None, prompt=main.AI_PROMPT), main.AI_PROMPT))
        self.assertFalse(main.is_prompt_reply(fake_message(), main.AI_PROMPT))
        self.assertFalse(main.is_prompt_reply(fake_message(prompt='Другой вопрос'), main.AI_PROMPT))
        self.assertFalse(main.is_prompt_reply(fake_message(prompt=main.AI_PROMPT, bot=False), main.AI_PROMPT))
        self.assertFalse(main.is_prompt_reply(fake_message(prompt=main.AI_PROMPT), main.AI_PROMPT, bot_id=99))
        message = fake_message(prompt=main.AI_PROMPT)
        message.reply_to_message.text = None
        self.assertFalse(main.is_prompt_reply(message, main.AI_PROMPT))
        message.reply_to_message.text = main.AI_PROMPT
        message.reply_to_message.from_user = None
        self.assertFalse(main.is_prompt_reply(message, main.AI_PROMPT))

    async def test_pdf_is_read_from_another_working_directory(self):
        message = fake_message()
        async def receive(document):
            self.assertTrue(document.read().startswith(b'%PDF'))
        message.reply_document.side_effect = receive
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                await main.send_pdf(message)
            finally:
                os.chdir(old_cwd)
        message.reply_document.assert_awaited_once()
        message.reply.assert_not_awaited()

    async def test_missing_pdf_has_friendly_reply(self):
        message = fake_message()
        with patch.object(main, 'PDF_PATH', Path('/nonexistent/test-info.pdf')):
            await main.send_pdf(message)
        message.reply.assert_awaited_once_with('PDF пока не загружен.')

    async def test_youtube_response_and_invalid_input(self):
        message = fake_message('https://youtu.be/example')
        await main.youtube_summary_handler(message)
        self.assertIn(message.text, message.reply.call_args.args[0])
        message = fake_message('invalid')
        await main.youtube_summary_handler(message)
        message.reply.assert_awaited_once_with('Не удалось получить саммари.')

    async def test_long_youtube_placeholder_is_split(self):
        message = fake_message('https://youtu.be/' + 'x' * 4070)
        await main.youtube_summary_handler(message)
        chunks = [call.args[0] for call in message.reply.await_args_list]
        self.assertEqual(''.join(chunks), handlers.handle_youtube_summary(message.text))
        self.assertTrue(all(len(chunk) <= 4096 for chunk in chunks))
        self.assertGreater(len(chunks), 1)

    async def test_form_request_does_not_block_event_loop(self):
        entered, release = threading.Event(), threading.Event()
        def slow_submission(data):
            entered.set()
            return release.wait(timeout=2)
        message = fake_message('Имя / 123 / текст')
        with patch.object(main, 'handle_google_form_submission', side_effect=slow_submission):
            task = asyncio.create_task(main.google_form_handler(message))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                # This point must be reachable while the submission is still waiting.
                self.assertFalse(task.done())
            finally:
                release.set()
                await task
        message.reply.assert_awaited_once_with('✅ Заявка отправлена!')

    async def test_repeat_form_is_not_sent_twice(self):
        message = fake_message('Имя / 123 / текст')
        with patch.object(main, 'handle_google_form_submission', return_value=True) as submit:
            await main.google_form_handler(message)
            await main.google_form_handler(message)
        submit.assert_called_once()
        self.assertIn('google_form', self.store.journal(1, 'legacy'))

    async def test_failed_form_response(self):
        message = fake_message('invalid')
        with patch.object(main, 'handle_google_form_submission', return_value=False):
            await main.google_form_handler(message)
        message.reply.assert_awaited_once_with('❌ Ошибка при отправке.')

    async def test_dispatcher_routes_actual_updates_without_telegram(self):
        dp = main.create_dispatcher(DUMMY_TOKEN)
        self.assertEqual(len(dp.message_handlers.handlers), 11)
        with patch.object(types.Message, 'answer', new_callable=AsyncMock) as answer, \
             patch.object(types.Message, 'reply', new_callable=AsyncMock) as reply, \
             patch.object(types.Message, 'reply_document', new_callable=AsyncMock) as document, \
             patch.object(main, 'OPENAI_API_KEY', None), \
             patch.object(main, 'handle_google_form_submission', return_value=True):
            async def deliver(text, prompt=None, photo=False):
                data = dict(message_id=1, date=0, chat={'id': 1, 'type': 'private'},
                            **{'from': {'id': 1, 'is_bot': False, 'first_name': 'Test'}})
                if photo:
                    data['photo'] = [{'file_id': 'x', 'file_unique_id': 'x', 'width': 1, 'height': 1}]
                else:
                    data['text'] = text
                if prompt is not None:
                    data['reply_to_message'] = dict(
                        message_id=2, date=0, chat={'id': 1, 'type': 'private'}, text=prompt,
                        **{'from': {'id': 123456789, 'is_bot': True, 'first_name': 'Bot'}},
                    )
                await dp.process_update(types.Update(update_id=1, message=data))
            await deliver('/start')
            answer.assert_awaited_once()
            for text in ('📹 YouTube-саммари', '💬 Задать вопрос AI', '📊 Отправить заявку'):
                await deliver(text)
                self.assertTrue(reply.call_args.kwargs['reply_markup'].force_reply)
            await deliver('https://youtube.com/watch?v=123')
            self.assertIn('Краткое саммари', reply.call_args.args[0])
            await deliver('Вопрос', main.AI_PROMPT)
            self.assertIn('AI пока не настроен', reply.call_args.args[0])
            await deliver('Имя / 123 / текст', main.FORM_PROMPT)
            self.assertEqual(reply.call_args.args[0], '✅ Заявка отправлена!')
            await deliver('📎 Получить PDF')
            document.assert_awaited_once()
            count = reply.await_count
            await deliver(None, main.AI_PROMPT, photo=True)
            await deliver('Другое сообщение')
            self.assertEqual(reply.await_count, count)


class OpenAITests(NoNetwork, unittest.IsolatedAsyncioTestCase):
    async def run_completion(self, content='Ответ', status=200, choices=True):
        requests_seen, clients = [], []
        async def respond(request):
            requests_seen.append(request)
            if status != 200:
                return httpx.Response(status, json={'error': {'message': 'offline failure',
                                                              'type': 'test_error'}})
            return httpx.Response(200, json={
                'id': 'chatcmpl-test', 'object': 'chat.completion', 'created': 1, 'model': 'gpt-4',
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content},
                             'finish_reason': 'stop'}] if choices else [],
            })
        def factory(**kwargs):
            kwargs['max_retries'] = 0
            client = AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(respond),
            ))
            clients.append(client)
            return client
        message = fake_message('Тестовый вопрос')
        self.request_id = getattr(self, 'request_id', 0) + 1
        message.message_id = self.request_id
        with patch.object(main, 'OPENAI_API_KEY', DUMMY_KEY), \
             patch.object(main, 'AsyncOpenAI', side_effect=factory):
            await main.ask_ai_handler(message)
        self.assertEqual(len(requests_seen), 1)
        request = requests_seen[0]
        self.assertEqual(request.url.path, '/v1/chat/completions')
        self.assertEqual(request.headers['authorization'], 'Bearer ' + DUMMY_KEY)
        self.assertEqual(json.loads(request.content), {
            'model': 'offline-model', 'max_tokens': 800, 'messages': [{'role': 'user', 'content': 'Тестовый вопрос'}],
        })
        self.assertTrue(clients[0].is_closed())
        return message

    async def test_404_explains_config_without_fallback_or_secret_leak(self):
        with self.assertLogs(main.logger, level='WARNING') as logs:
            message = await self.run_completion(status=404)
        self.assertIn('OPENAI_MODEL', message.reply.call_args.args[0])
        self.assertIn('404', message.reply.call_args.args[0])
        self.assertNotIn(DUMMY_KEY, ''.join(logs.output))
        self.assertIn('api_404', self.store.journal(1, 'ai'))

    async def test_model_is_required_and_no_client_is_created(self):
        message = fake_message()
        with patch.object(main, 'OPENAI_API_KEY', DUMMY_KEY), \
             patch.object(main, 'OPENAI_MODEL', ''), patch.object(main, 'AsyncOpenAI') as client:
            await main.ask_ai_handler(message)
        client.assert_not_called()
        self.assertIn('OPENAI_MODEL', message.reply.call_args.args[0])

    async def test_repeat_ai_request_does_not_spend_twice(self):
        message = await self.run_completion()
        with patch.object(main, 'AsyncOpenAI') as client:
            await main.ask_ai_handler(message)
        client.assert_not_called()
        self.assertIn('заблокировано', message.reply.call_args.args[0])

    async def test_ai_daily_limit_blocks_client_and_unauthorized_access(self):
        message = fake_message()
        with patch.dict(os.environ, {'RAM_AI_DAILY_REQUESTS': '0'}), \
             patch.object(main, 'OPENAI_API_KEY', DUMMY_KEY), patch.object(main, 'AsyncOpenAI') as client:
            await main.ask_ai_handler(message)
        client.assert_not_called()
        self.assertIn('лимит', message.reply.call_args.args[0])
        message.from_user.id = 99
        with patch.object(main, 'AsyncOpenAI') as client:
            await main.ask_ai_handler(message)
        client.assert_not_called()
        self.assertIn('Доступ закрыт', message.reply.call_args.args[0])

    async def test_real_sdk_constructor_is_compatible_with_httpx(self):
        # Construction/cleanup only; the network guard also covers this client.
        async with AsyncOpenAI(api_key=DUMMY_KEY, timeout=30, max_retries=1) as client:
            self.assertFalse(client.is_closed())
        self.assertTrue(client.is_closed())

    async def test_modern_sdk_request_and_answer(self):
        message = await self.run_completion()
        message.reply.assert_awaited_once_with('Ответ')

    async def test_missing_key_makes_no_sdk_client(self):
        message = fake_message()
        with patch.object(main, 'OPENAI_API_KEY', None), patch.object(main, 'AsyncOpenAI') as client:
            await main.ask_ai_handler(message)
        client.assert_not_called()
        self.assertIn('AI пока не настроен', message.reply.call_args.args[0])

    async def test_nontext_question_makes_no_sdk_client(self):
        message = fake_message(None)
        with patch.object(main, 'AsyncOpenAI') as client:
            await main.ask_ai_handler(message)
        client.assert_not_called()
        message.reply.assert_awaited_once_with('Напиши вопрос текстом.')

    async def test_empty_content_and_missing_choices(self):
        for content, choices in ((None, True), ('', True), ('  ', True), ('', False)):
            with self.subTest(content=content, choices=choices):
                message = await self.run_completion(content, choices=choices)
                self.assertIn('пустой ответ', message.reply.call_args.args[0])

    async def test_api_errors_have_friendly_response(self):
        for status in (401, 429, 500):
            with self.subTest(status=status), self.assertLogs(main.logger, level='WARNING') as logs:
                message = await self.run_completion(status=status)
                message.reply.assert_awaited_once_with('Не удалось получить ответ AI. Попробуй позже.')
                self.assertNotIn(DUMMY_KEY, ''.join(logs.output))
                self.assertNotIn('offline failure', ''.join(logs.output))

    async def test_long_answer_keeps_all_content_in_valid_sized_messages(self):
        content = 'а' * 4095 + '😀' * 3000
        message = await self.run_completion(content)
        chunks = [call.args[0] for call in message.reply.await_args_list]
        self.assertEqual(''.join(chunks), content)
        self.assertTrue(all(0 < len(chunk.encode('utf-16-le')) // 2 <= 4096 for chunk in chunks))

    async def test_connection_timeout_has_friendly_response(self):
        def factory(**kwargs):
            async def timeout(request):
                raise httpx.ReadTimeout('offline timeout', request=request)
            return AsyncOpenAI(api_key=DUMMY_KEY, max_retries=0,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(timeout)))
        message = fake_message()
        with patch.object(main, 'OPENAI_API_KEY', DUMMY_KEY), \
             patch.object(main, 'AsyncOpenAI', side_effect=factory), \
             self.assertLogs(main.logger, level='WARNING'):
            await main.ask_ai_handler(message)
        message.reply.assert_awaited_once_with('Не удалось получить ответ AI. Попробуй позже.')


if __name__ == '__main__':
    unittest.main()
