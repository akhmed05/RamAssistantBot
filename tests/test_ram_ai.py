"""Offline contract and safety tests. Never connect to Telegram/WB/OpenAI."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'RamAssistantBot'))
import httpx
from aiogram import types
from aiogram.dispatcher.handler import CancelHandler
import main
from ram_ai import Cabinet, Chief, Controller, SafeError, Store, Wildberries, reserve_ai, summarize
from ram_telegram import OwnerMiddleware, RamTelegram

TOKEN = 'offline-wb-secret'
BOT = '123456789:' + 'A' * 35


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')
        self.cabs = {n: Cabinet(n, 'Кабинет ' + n, TOKEN + n, frozenset({'sales', 'stocks'})) for n in ('1', '2')}
        env = patch.dict(os.environ, {'RAM_OWNER_IDS': '10', 'RAM_WB_DAILY_REQUESTS': '200',
                                    'RAM_AI_DAILY_REQUESTS': '30', 'RAM_AI_DAILY_TOKEN_BUDGET': '60000'})
        env.start()
        self.addCleanup(env.stop)
        for target in ('socket.socket.connect', 'socket.getaddrinfo'):
            p = patch(target, side_effect=AssertionError('Network forbidden'))
            p.start()
            self.addCleanup(p.stop)


class StorageTests(Base):
    def test_selection_requires_explicit_account_and_is_persistent(self):
        with self.assertRaises(SafeError):
            self.store.selected(10)
        self.store.select(10, '1')
        self.store.select(11, '2')
        reopened = Store(self.store.path)
        self.assertEqual(reopened.selected(10), '1')
        self.assertEqual(reopened.selected(11), '2')
        with self.assertRaises(SafeError):
            self.store.select(10, '3')

    def test_claim_and_results_survive_restart(self):
        self.assertIsNone(self.store.claim('a', 10, '1', 'sales'))
        self.assertIn('заблокировано', Store(self.store.path).claim('a', 10, '1', 'sales'))
        self.store.finish('a', 10, '1', 'sales', 'done', 'answer')
        self.assertEqual(Store(self.store.path).claim('a', 10, '1', 'sales'), 'answer')
        with self.assertRaises(SafeError):
            self.store.claim('a', 10, '2', 'sales')
        self.assertNotIn('sales', self.store.journal(10, '2'))

    def test_daily_budget_and_rate_persist_and_do_not_cross_accounts(self):
        self.store.reserve('budget:1', 1, 1)
        with self.assertRaises(SafeError):
            Store(self.store.path).reserve('budget:1', 1, 1)
        self.store.reserve('budget:2', 1, 1)
        self.store.gate('rate:1', 65, now=100)
        with self.assertRaises(SafeError):
            Store(self.store.path).gate('rate:1', 65, now=101)
        self.store.gate('rate:2', 65, now=101)
        self.store.gate('rate:1', 65, now=165)

    def test_draft_confirmation_is_single_use_bound_to_owner_and_original_cabinet(self):
        ident = self.store.draft(10, '1', 'cards')
        with self.assertRaises(SafeError):
            self.store.decide(ident, 11, True)
        self.store.select(10, '2')
        result = self.store.decide(ident, 10, True)
        self.assertIn('Кабинет 1', result)
        self.assertIn('не выполнялась', result)
        with self.assertRaises(SafeError):
            self.store.decide(ident, 10, True)
        self.assertIn('approved_draft', self.store.journal(10, '1'))

    def test_expired_or_rejected_draft_never_approves(self):
        ident = self.store.draft(10, '1', 'ads')
        with self.store.connect() as db:
            db.execute('UPDATE drafts SET expires=0 WHERE id=?', (ident,))
        with self.assertRaises(SafeError):
            self.store.decide(ident, 10, True)
        ident = self.store.draft(10, '1', 'ads')
        self.assertIn('отклонён', self.store.decide(ident, 10, False))
        with self.assertRaises(SafeError):
            self.store.decide(ident, 10, True)

    def test_ai_budget_reserves_before_call(self):
        with patch.dict(os.environ, {'RAM_AI_DAILY_REQUESTS': '1'}):
            reserve_ai(self.store, 'question', 800)
            with self.assertRaises(SafeError):
                reserve_ai(self.store, 'question', 800)
        with patch.dict(os.environ, {'RAM_AI_DAILY_TOKEN_BUDGET': '1'}):
            with self.assertRaises(SafeError):
                reserve_ai(Store(Path(self.temp.name) / 'second.db'), 'q', 800)


class AnalysisTests(Base):
    def test_return_direction_and_period_and_moscow_dates(self):
        since = datetime.now(timezone.utc) - timedelta(days=7)
        recent = datetime.now(timezone.utc).isoformat()
        data = [{'date': recent, 'saleID': 'S1', 'forPay': 100},
                {'date': recent, 'saleID': 'R1', 'forPay': 20},
                {'date': (since - timedelta(days=1)).isoformat(), 'saleID': 'S2', 'forPay': 999}]
        result = summarize('sales', data, since)
        self.assertIn('80.00', result)
        self.assertIn('Продажи за последние 7 суток: 1, возвраты: 1', result)
        self.assertIn('не прибыль', result)
        data[0]['date'] = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None).isoformat()
        self.assertIn('80.00', summarize('sales', data, since))

    def test_malformed_or_unsafe_response_fails_closed(self):
        for data in ({}, [{}], [{'nmId': 1, 'quantity': '10'}], [{'nmId': 1, 'quantity': True}],
                     [{'nmId': 1, 'quantity': -1}], [{'nmId': 1, 'quantity': float('nan')}]):
            with self.subTest(data=data), self.assertRaises(SafeError):
                Controller.validate('stocks', data)
        with self.assertRaises(SafeError):
            summarize('sales', [{'date': 'bad', 'saleID': 'S1', 'forPay': 1}], datetime.now(timezone.utc))
        with self.assertRaises(SafeError):
            summarize('sales', [{'date': datetime.now(timezone.utc).isoformat(), 'saleID': 'X1', 'forPay': 1}], datetime.now(timezone.utc))

    def test_partial_response_explicitly_marked(self):
        result = summarize('stocks', [{'nmId': 1, 'quantity': 1}] * 60000, datetime.now(timezone.utc))
        self.assertIn('неполный', result)


class WBTests(Base, unittest.IsolatedAsyncioTestCase):
    def chief(self, handler):
        return Chief(self.store, self.cabs, httpx.MockTransport(handler))

    async def test_official_get_credentials_separate_cache_and_dedup(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, json=[{'nmId': 1, 'quantity': len(calls)}])
        chief = self.chief(handle)
        first = await chief.run('job1', 10, '1', 'stocks')
        self.assertIn('1 шт.', first)
        self.assertEqual(await chief.run('job1', 10, '1', 'stocks'), first)
        self.assertIn('кэш', await chief.run('job2', 10, '1', 'stocks'))
        self.assertIn('2 шт.', await chief.run('job3', 10, '2', 'stocks'))
        self.assertEqual(len(calls), 2)
        for n, req in enumerate(calls, 1):
            self.assertEqual(req.method, 'GET')
            self.assertEqual(req.url.host, 'statistics-api.wildberries.ru')
            self.assertEqual(req.url.path, '/api/v1/supplier/stocks')
            self.assertEqual(req.url.params['dateFrom'], '2019-06-20')
            self.assertEqual(req.headers['Authorization'], TOKEN + str(n))
        with self.store.connect() as db:
            records = str(db.execute('SELECT * FROM events').fetchall()) + str(db.execute('SELECT * FROM jobs').fetchall())
        self.assertNotIn(TOKEN, records)
        self.assertNotIn(TOKEN, repr(self.cabs))

    async def test_sales_endpoint_query_and_no_redirects(self):
        calls = []
        def respond(req):
            calls.append(req)
            return httpx.Response(302, headers={'Location': 'https://evil.example'})
        text = await self.chief(respond).run('j', 10, '1', 'sales')
        self.assertIn('HTTP 302', text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].url.path, '/api/v1/supplier/sales')
        self.assertEqual(calls[0].url.params['flag'], '0')

    async def test_permissions_and_missing_token_and_write_block_network(self):
        def fail(req):
            raise AssertionError('Unexpected HTTP')
        chief = self.chief(fail)
        for action in ('delete', 'ads', 'price', 'https://evil.example'):
            with self.assertRaises(SafeError):
                await chief.run(action, 10, '1', action)
        chief.cabinets = {'1': Cabinet('1', 'one', '', frozenset({'stocks'}))}
        with self.assertRaises(SafeError):
            await chief.run('x', 10, '1', 'stocks')
        chief.cabinets = {'1': Cabinet('1', 'one', TOKEN, frozenset())}
        with self.assertRaises(SafeError):
            await chief.run('y', 10, '1', 'stocks')

    async def test_http_failures_not_retried_and_sensitive_body_not_forwarded(self):
        for status in (401, 403, 404, 429, 500):
            with self.subTest(status=status):
                store = Store(Path(self.temp.name) / f'{status}.db')
                calls = []
                def respond(req):
                    calls.append(req)
                    return httpx.Response(status, text=TOKEN, headers={'Retry-After': '120'})
                chief = Chief(store, self.cabs, httpx.MockTransport(respond))
                result = await chief.run('a', 10, '1', 'stocks')
                self.assertNotIn(TOKEN, result)
                self.assertEqual(len(calls), 1)
                self.assertEqual(await chief.run('a', 10, '1', 'stocks'), result)
                self.assertIn('failed', store.journal(10, '1'))
                await chief.run('b', 10, '1', 'stocks')
                self.assertEqual(len(calls), 1)
                if status == 429:
                    with store.connect() as db:
                        value = db.execute("SELECT value FROM limits WHERE key='wb-rate:1:stocks'").fetchone()[0]
                    self.assertGreater(value, time.time() + 110)

    async def test_timeout_and_invalid_json_safe(self):
        def timeout(req):
            raise httpx.ReadTimeout(TOKEN, request=req)
        result = await self.chief(timeout).run('a', 10, '1', 'stocks')
        self.assertNotIn(TOKEN, result)
        self.assertIn('таймаут', result)
        chief = Chief(Store(Path(self.temp.name) / 'invalid.db'), self.cabs,
                      httpx.MockTransport(lambda r: httpx.Response(200, text='not json ' + TOKEN)))
        self.assertIn('JSON', await chief.run('a', 10, '1', 'stocks'))

    async def test_concurrent_duplicate_job_calls_once(self):
        calls = []
        async def respond(req):
            calls.append(req)
            await asyncio.sleep(0)
            return httpx.Response(200, json=[])
        chief = self.chief(respond)
        result = await asyncio.gather(chief.run('same', 10, '1', 'stocks'), chief.run('same', 10, '1', 'stocks'))
        self.assertEqual(len(calls), 1)
        self.assertTrue(any('заблокировано' in r for r in result))


class TelegramTests(Base, unittest.IsolatedAsyncioTestCase):
    def query(self, data, owner=10, ident='q1'):
        return SimpleNamespace(id=ident, data=data, from_user=SimpleNamespace(id=owner), answer=AsyncMock(),
                               message=SimpleNamespace(chat=SimpleNamespace(type='private'), answer=AsyncMock()))

    async def test_buttons_acknowledge_then_dispatch_and_bind_account(self):
        chief = self.chief_mock()
        ui = RamTelegram(self.store, chief)
        for n in ('1', '2'):
            query = self.query('ram:select:' + n)
            await ui.callback(query)
            query.answer.assert_awaited_once()
            self.assertEqual(self.store.selected(10), n)
        query = self.query('ram:read:stocks')
        await ui.callback(query)
        chief.run.assert_awaited_once_with('callback:q1', 10, '2', 'stocks')
        for data in ('ram:status', 'ram:agents', 'ram:journal', 'ram:help', 'ram:unknown', 'ram:read:price'):
            query = self.query(data)
            await ui.callback(query)
            query.answer.assert_awaited_once()
            query.message.answer.assert_awaited_once()

    def chief_mock(self):
        return SimpleNamespace(cabinets=self.cabs, run=AsyncMock(return_value='report'))

    async def test_unauthorized_callback_and_missing_selection_never_call_wb(self):
        chief = self.chief_mock()
        ui = RamTelegram(self.store, chief)
        await ui.callback(self.query('ram:read:stocks', owner=11))
        await ui.callback(self.query('ram:read:stocks'))
        chief.run.assert_not_awaited()

    async def test_draft_buttons_and_confirm_do_not_execute_wb(self):
        chief = self.chief_mock()
        ui = RamTelegram(self.store, chief)
        self.store.select(10, '1')
        query = self.query('ram:draft:cards')
        await ui.callback(query)
        buttons = query.message.answer.call_args.kwargs['reply_markup'].inline_keyboard[0]
        await ui.callback(query)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM drafts').fetchone()[0], 1)
        approved = self.query(buttons[0].callback_data)
        await ui.callback(approved)
        self.assertIn('не выполнялась', approved.message.answer.call_args.args[0])
        await ui.callback(approved)
        self.assertIn('уже рассмотрен', approved.message.answer.call_args.args[0])
        chief.run.assert_not_awaited()

    async def test_owner_middleware_rejects_unconfigured_and_group_access(self):
        middleware = OwnerMiddleware()
        for owner, chat in ((11, 'private'), (10, 'group')):
            message = SimpleNamespace(from_user=SimpleNamespace(id=owner), chat=SimpleNamespace(type=chat), reply=AsyncMock())
            with self.assertRaises(CancelHandler):
                await middleware.on_pre_process_message(message, {})
        with patch.dict(os.environ, {'RAM_OWNER_IDS': ''}):
            message = SimpleNamespace(from_user=SimpleNamespace(id=10), chat=SimpleNamespace(type='private'), reply=AsyncMock())
            with self.assertRaises(CancelHandler):
                await middleware.on_pre_process_message(message, {})

    async def test_real_dispatcher_callback_and_command_routing(self):
        with patch.object(main, '_store', self.store):
            dp = main.create_dispatcher(BOT)
        with patch.object(types.CallbackQuery, 'answer', new_callable=AsyncMock) as ack, \
             patch.object(types.Message, 'answer', new_callable=AsyncMock) as answer, \
             patch.object(types.Message, 'reply', new_callable=AsyncMock) as reply:
            message = {'message_id': 1, 'date': 0, 'chat': {'id': 10, 'type': 'private'},
                       'from': {'id': 10, 'is_bot': False, 'first_name': 'owner'}, 'text': '/ram'}
            await dp.process_update(types.Update(update_id=100, message=message))
            self.assertIn('RAM AI', answer.call_args.args[0])
            await dp.process_update(types.Update(update_id=101, callback_query={
                'id': 'c1', 'from': message['from'], 'chat_instance': 'test', 'message': message, 'data': 'ram:select:2'}))
            ack.assert_awaited_once()
            self.assertEqual(self.store.selected(10), '2')
            message['text'] = '/task реклама'
            await dp.process_update(types.Update(update_id=102, message=message))
            self.assertIn('Черновик', answer.call_args.args[0])
            message['from'] = {'id': 11, 'is_bot': False, 'first_name': 'outsider'}
            count = answer.await_count
            await dp.process_update(types.Update(update_id=103, message=message))
            self.assertEqual(answer.await_count, count)
            self.assertIn('Доступ закрыт', reply.call_args.args[0])
