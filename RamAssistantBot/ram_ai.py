"""RAM AI read-only core. No Telegram objects or remote writes in this module."""
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx


class SafeError(Exception):
    """Only fixed, credential-free messages may cross the UI boundary."""


@dataclass(frozen=True)
class Cabinet:
    id: str
    name: str
    token: str = field(repr=False)
    scopes: frozenset = frozenset()


def cabinets_from_env():
    return {str(n): Cabinet(str(n), os.getenv(f'RAM_WB_{n}_NAME', f'Кабинет {n}'),
                           os.getenv(f'RAM_WB_{n}_STATISTICS_TOKEN', ''),
                           frozenset(x.strip() for x in os.getenv(f'RAM_WB_{n}_READ_SCOPES', '').split(',') if x.strip()))
            for n in (1, 2)}


def owner_ids():
    return frozenset(int(x.strip()) for x in os.getenv('RAM_OWNER_IDS', '').split(',') if x.strip())


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS selection(owner INTEGER PRIMARY KEY, cabinet TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, owner INTEGER, cabinet TEXT,
                action TEXT, status TEXT, result TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, owner INTEGER, cabinet TEXT,
                action TEXT, status TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS limits(key TEXT PRIMARY KEY, value REAL);
            CREATE TABLE IF NOT EXISTS cache(cabinet TEXT, action TEXT, result TEXT, created REAL,
                PRIMARY KEY(cabinet,action));
            CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY, owner INTEGER, cabinet TEXT,
                role TEXT, status TEXT, expires REAL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def select(self, owner, cabinet):
        if cabinet not in {'1', '2'}:
            raise SafeError('Неизвестный кабинет.')
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO selection VALUES (?,?)', (owner, cabinet))

    def selected(self, owner):
        with self.connect() as db:
            row = db.execute('SELECT cabinet FROM selection WHERE owner=?', (owner,)).fetchone()
        if not row:
            raise SafeError('Сначала выберите кабинет 1 или 2.')
        return row[0]

    def claim(self, job, owner, cabinet, action):
        with self.connect() as db:
            cursor = db.execute('INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?,?,?)',
                                (job, owner, cabinet, action, 'running', None, time.time()))
            if cursor.rowcount:
                db.execute('INSERT INTO events(owner,cabinet,action,status,created) VALUES (?,?,?,?,?)',
                           (owner, cabinet, action, 'running', time.time()))
                return None
            row = db.execute('SELECT owner,cabinet,action,status,result FROM jobs WHERE id=?', (job,)).fetchone()
            if row[:3] != (owner, cabinet, action):
                raise SafeError('Повторное задание относится к другому контексту.')
            return row[4] or 'Задание уже принято. Повторное выполнение заблокировано; проверьте журнал.'

    def finish(self, job, owner, cabinet, action, status, result):
        with self.connect() as db:
            db.execute('UPDATE jobs SET status=?,result=? WHERE id=?', (status, result, job))
            db.execute('INSERT INTO events(owner,cabinet,action,status,created) VALUES (?,?,?,?,?)',
                       (owner, cabinet, action, status, time.time()))

    def reserve(self, key, amount, maximum):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value FROM limits WHERE key=?', (key,)).fetchone()
            value = (row[0] if row else 0) + amount
            if value > maximum:
                raise SafeError('Достигнут суточный лимит API. Попробуйте завтра.')
            db.execute('INSERT OR REPLACE INTO limits VALUES (?,?)', (key, value))

    def gate(self, key, interval, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value FROM limits WHERE key=?', (key,)).fetchone()
            if row and row[0] > now:
                raise SafeError(f'Лимит WB: повторите через {math.ceil(row[0] - now)} сек.')
            db.execute('INSERT OR REPLACE INTO limits VALUES (?,?)', (key, now + interval))

    def cooldown(self, key, seconds):
        with self.connect() as db:
            db.execute('INSERT INTO limits VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=max(value,excluded.value)',
                       (key, time.time() + seconds))

    def cached(self, cabinet, action, ttl=65):
        with self.connect() as db:
            row = db.execute('SELECT result,created FROM cache WHERE cabinet=? AND action=?', (cabinet, action)).fetchone()
        return row[0] if row and time.time() - row[1] < ttl else None

    def put_cache(self, cabinet, action, result):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?,?)', (cabinet, action, result, time.time()))

    def journal(self, owner, cabinet):
        with self.connect() as db:
            rows = db.execute('SELECT action,status,created FROM events WHERE owner=? AND cabinet=? ORDER BY id DESC LIMIT 10',
                              (owner, cabinet)).fetchall()
        return '\n'.join(f'{datetime.fromtimestamp(t, timezone.utc):%Y-%m-%d %H:%M} UTC: {a} — {s}'
                         for a, s, t in rows) or 'Журнал пуст.'

    def draft(self, owner, cabinet, role):
        ident = uuid.uuid4().hex[:16]
        with self.connect() as db:
            db.execute('INSERT INTO drafts VALUES (?,?,?,?,?,?)', (ident, owner, cabinet, role, 'pending', time.time() + 900))
        return ident

    def decide(self, ident, owner, approve):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT cabinet,role,status,expires FROM drafts WHERE id=? AND owner=?', (ident, owner)).fetchone()
            if not row or row[2] != 'pending' or row[3] < time.time():
                raise SafeError('Черновик недоступен, истёк или уже рассмотрен.')
            status = 'approved_draft' if approve else 'rejected'
            db.execute('UPDATE drafts SET status=? WHERE id=?', (status, ident))
            db.execute('INSERT INTO events(owner,cabinet,action,status,created) VALUES (?,?,?,?,?)',
                       (owner, row[0], row[1], status, time.time()))
        return f'Кабинет {row[0]}: ' + ('черновик согласован. Запись в WB не реализована и не выполнялась.' if approve else 'черновик отклонён.')


class Controller:
    @staticmethod
    def allow(cabinet, action):
        if action not in {'sales', 'stocks'}:
            raise SafeError('Контролёр запрещает запись в WB в этой версии.')
        if action not in cabinet.scopes:
            raise SafeError('Нет разрешения на этот тип чтения в выбранном кабинете.')
        if not cabinet.token:
            raise SafeError('Ключ Statistics API этого кабинета не настроен в Railway.')

    @staticmethod
    def validate(action, data):
        fields = {'sales': ('date', 'saleID', 'forPay'), 'stocks': ('nmId', 'quantity')}[action]
        if not isinstance(data, list) or any(not isinstance(row, dict) or any(f not in row for f in fields) for row in data):
            raise SafeError('Контролёр: неожиданный формат WB, отчёт не построен.')
        numeric = 'forPay' if action == 'sales' else 'quantity'
        if any(isinstance(row[numeric], bool) or not isinstance(row[numeric], (int, float)) or not math.isfinite(row[numeric]) for row in data):
            raise SafeError('Контролёр: некорректные числа WB.')
        if action == 'stocks' and any(row['quantity'] < 0 for row in data):
            raise SafeError('Контролёр: отрицательные остатки WB.')


class Wildberries:
    HOST = 'https://statistics-api.wildberries.ru'
    PATHS = {'sales': '/api/v1/supplier/sales', 'stocks': '/api/v1/supplier/stocks'}

    def __init__(self, store, transport=None):
        self.store, self.transport = store, transport

    async def read(self, cabinet, action):
        Controller.allow(cabinet, action)
        cached = self.store.cached(cabinet.id, action)
        if cached:
            return cached + '\nИсточник: кэш менее 65 сек.'
        key = f'wb-rate:{cabinet.id}:{action}'
        # Statistics endpoints: at most one request/minute; 65s conservative gap.
        self.store.gate(key, 65)
        day = datetime.now(timezone.utc).date().isoformat()
        self.store.reserve(f'wb-budget:{cabinet.id}:{day}', 1, int(os.getenv('RAM_WB_DAILY_REQUESTS', '200')))
        since = (datetime.now(timezone.utc) - timedelta(days=7)).replace(microsecond=0)
        params = {'dateFrom': since.isoformat()}
        if action == 'sales':
            params['flag'] = 0
        else:
            params['dateFrom'] = '2019-06-20'
        try:
            async with httpx.AsyncClient(timeout=30, transport=self.transport, follow_redirects=False) as client:
                response = await client.get(self.HOST + self.PATHS[action], params=params,
                                            headers={'Authorization': cabinet.token})
        except httpx.HTTPError:
            raise SafeError('WB: сеть недоступна или таймаут. Автоповтора нет.') from None
        if response.status_code == 429:
            try:
                wait = min(86400, max(65, int(response.headers.get('Retry-After', '65'))))
            except ValueError:
                wait = 65
            self.store.cooldown(key, wait)
            raise SafeError('WB ограничил частоту запросов (429). Повторите позже.')
        if response.status_code in {401, 403}:
            raise SafeError('WB отказал в доступе. Проверьте ключ и категорию «Статистика» в Railway.')
        if response.status_code != 200:
            raise SafeError(f'WB вернул HTTP {response.status_code}. Отчёт не построен.')
        try:
            data = response.json()
        except ValueError:
            raise SafeError('WB вернул некорректный JSON.') from None
        Controller.validate(action, data)
        result = summarize(action, data, since)
        self.store.put_cache(cabinet.id, action, result)
        return result


def summarize(action, data, since):
    partial = len(data) >= 60000
    warning = '\nВНИМАНИЕ: возможен предел 60 000 строк; отчёт неполный, пагинация в MVP не реализована.' if partial else ''
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    if action == 'stocks':
        return (f'Остатки WB (склады WB, не FBS): {sum(r["quantity"] for r in data):,.0f} шт.\n'
                f'Строк склад/SKU/размер: {len(data)}. Получено: {stamp}.\n'
                'WB обновляет статистику с задержкой; это не остатки в реальном времени.' + warning)
    rows = []
    for row in data:
        try:
            date = datetime.fromisoformat(row['date'].replace('Z', '+00:00'))
            # WB dates without offset are Moscow time (UTC+3).
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone(timedelta(hours=3)))
            if not isinstance(row['saleID'], str) or row['saleID'][:1] not in {'S', 'R'}:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise SafeError('Контролёр: неизвестная дата или тип продажи WB.') from None
        if since <= date <= datetime.now(timezone.utc):
            rows.append(row)
    sales = [r for r in rows if r['saleID'].startswith('S')]
    returns = [r for r in rows if r['saleID'].startswith('R')]
    # WB forPay is positive for both S and R; saleID carries the direction.
    net = sum(abs(r['forPay']) for r in sales) - sum(abs(r['forPay']) for r in returns)
    return (f'Продажи за последние 7 суток: {len(sales)}, возвраты: {len(returns)}.\n'
            f'К перечислению по оперативной статистике: {net:,.2f} ₽.\n'
            f'Строк ответа: {len(data)}. Получено: {stamp}.\n'
            'Это не прибыль: нет себестоимости, рекламы и полного отчёта расходов. '
            'dateFrom фильтрует изменения; данные могут уточняться WB.' + warning)


class Chief:
    def __init__(self, store, cabinets=None, transport=None):
        self.store = store
        self.cabinets = cabinets if cabinets is not None else cabinets_from_env()
        self.wb = Wildberries(store, transport)

    async def run(self, job, owner, cabinet_id, action):
        cabinet = self.cabinets.get(cabinet_id)
        if cabinet is None:
            raise SafeError('Неизвестный кабинет.')
        Controller.allow(cabinet, action)
        previous = self.store.claim(job, owner, cabinet_id, action)
        if previous is not None:
            return previous
        try:
            result = await self.wb.read(cabinet, action)
            result = f'{cabinet.name} (ID {cabinet.id})\nПлан: чтение WB → аналитик → контролёр.\n' + result
            status = 'done'
        except SafeError as exc:
            result, status = str(exc), 'failed'
        except Exception:
            # Never forward raw request/body/exception text or credentials.
            result, status = 'Внутренняя ошибка. Проверьте журнал; автоповтора нет.', 'failed'
        self.store.finish(job, owner, cabinet_id, action, status, result)
        return result


def reserve_ai(store, text, output_tokens):
    day = datetime.now(timezone.utc).date().isoformat()
    store.reserve(f'ai-requests:{day}', 1, int(os.getenv('RAM_AI_DAILY_REQUESTS', '30')))
    # Reserve conservatively BEFORE sending; failed calls also consume allowance.
    store.reserve(f'ai-tokens:{day}', len(text.encode('utf-8')) + 256 + output_tokens,
                  int(os.getenv('RAM_AI_DAILY_TOKEN_BUDGET', '60000')))
