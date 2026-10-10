"""Owner-only Telegram interface, registered alongside the existing menu."""
from aiogram import types
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.dispatcher.handler import CancelHandler

from ram_ai import Chief, SafeError, Store, owner_ids

ROLES = {
    'chief': 'Главный агент: планирование и сбор результатов',
    'analyst': 'Аналитик WB: продажи и остатки (работает)',
    'cards': 'Менеджер карточек: подготовка задачи (интеграция ещё не подключена)',
    'ads': 'Рекламный агент: подготовка задачи (интеграция ещё не подключена)',
    'content': 'Контент-агент: подготовка задачи (генерация ещё не подключена)',
    'finance': 'Финансовый агент: подготовка задачи (расчёты ещё не подключены)',
    'controller': 'Контролёр: запрет записи и проверка формата данных (работает)',
}


def authorized(message):
    return bool(message and message.from_user and message.from_user.id in owner_ids()
                and message.chat.type == 'private')


class OwnerMiddleware(BaseMiddleware):
    """All existing paid/external features and business data become owner-only."""
    async def on_pre_process_message(self, message, data):
        if not authorized(message):
            await message.reply('Доступ закрыт. Владелец настраивается через RAM_OWNER_IDS в Railway.')
            raise CancelHandler()

    async def on_pre_process_callback_query(self, query, data):
        if not query.message or query.from_user.id not in owner_ids() or query.message.chat.type != 'private':
            await query.answer('Доступ закрыт.', show_alert=True)
            raise CancelHandler()


def menu():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.row(types.InlineKeyboardButton('Кабинет 1', callback_data='ram:select:1'),
                 types.InlineKeyboardButton('Кабинет 2', callback_data='ram:select:2'))
    keyboard.row(types.InlineKeyboardButton('Продажи · 7 суток', callback_data='ram:read:sales'),
                 types.InlineKeyboardButton('Остатки WB', callback_data='ram:read:stocks'))
    keyboard.row(types.InlineKeyboardButton('Агенты', callback_data='ram:agents'),
                 types.InlineKeyboardButton('Статус', callback_data='ram:status'))
    keyboard.row(types.InlineKeyboardButton('Журнал', callback_data='ram:journal'),
                 types.InlineKeyboardButton('Новая задача', callback_data='ram:help'))
    return keyboard


class RamTelegram:
    def __init__(self, store, chief=None):
        self.store = store
        self.chief = chief if chief is not None else Chief(store)

    async def open(self, message):
        if not authorized(message):
            await message.reply('Доступ закрыт.')
            return
        await message.answer('RAM AI · чтение и подготовка. Выберите кабинет, затем задачу.', reply_markup=menu())

    async def send(self, message, text, markup=None):
        # Fixed summaries are short; user-provided cabinet labels are bounded by config validation.
        await message.answer(text, reply_markup=markup)

    def status(self, owner):
        try:
            selected = self.store.selected(owner)
        except SafeError:
            selected = 'не выбран'
        rows = ['RAM AI: запись WB отключена.', f'Выбранный кабинет: {selected}.']
        for c in self.chief.cabinets.values():
            rows.append(f'{c.name} ({c.id}): ключ ' + ('настроен' if c.token else 'не настроен')
                        + '; права: ' + (', '.join(sorted(c.scopes)) or 'нет'))
        rows.append('Наличие ключа не подтверждает доступ WB. Проверьте чтением.\n'
                    'Карточки/реклама/контент/финансы: только подготовка задачи.')
        return '\n'.join(rows)

    async def callback(self, query):
        # Acknowledge BEFORE network work so Telegram buttons do not keep spinning.
        await query.answer()
        if not query.message or query.from_user.id not in owner_ids() or query.message.chat.type != 'private':
            return
        owner = query.from_user.id
        parts = (query.data or '').split(':')
        try:
            if parts[:2] == ['ram', 'select'] and len(parts) == 3:
                self.store.select(owner, parts[2])
                await self.send(query.message, f'Выбран кабинет {parts[2]}.', menu())
            elif parts == ['ram', 'status']:
                await self.send(query.message, self.status(owner), menu())
            elif parts == ['ram', 'agents']:
                buttons = types.InlineKeyboardMarkup(row_width=1)
                for role in ('cards', 'ads', 'content', 'finance'):
                    buttons.add(types.InlineKeyboardButton('Подготовить: ' + ROLES[role].split(':')[0],
                                                          callback_data=f'ram:draft:{role}'))
                await self.send(query.message, '\n'.join(ROLES.values()), buttons)
            elif parts == ['ram', 'journal']:
                cabinet = self.store.selected(owner)
                await self.send(query.message, f'Кабинет {cabinet}\n' + self.store.journal(owner, cabinet) + '\nOpenAI (общий лимит):\n' + self.store.journal(owner, 'ai') + '\nПрежние внешние действия:\n' + self.store.journal(owner, 'legacy'))
            elif parts == ['ram', 'help']:
                await self.send(query.message, 'Команды: /ram, /task продажи, /task остатки.\n'
                                'Период продаж: последние 7 суток. Кабинет выберите кнопкой.\n'
                                'Другие задачи: /task карточки, /task реклама, /task контент, /task финансы.\n'
                                'Это создаёт черновик плана без исполнения. Не присылайте API-ключи.')
            elif parts[:2] == ['ram', 'read'] and len(parts) == 3 and parts[2] in {'sales', 'stocks'}:
                cabinet = self.store.selected(owner)
                # Callback ID is globally unique; persisted across process restarts.
                result = await self.chief.run('callback:' + query.id, owner, cabinet, parts[2])
                await self.send(query.message, result, menu())
            elif parts[:2] == ['ram', 'draft'] and len(parts) == 3 and parts[2] in {'cards', 'ads', 'content', 'finance'}:
                await self.prepare(query.message, owner, parts[2], "callback:" + query.id)
            elif parts[:2] == ['ram', 'decide'] and len(parts) == 4 and parts[3] in {'yes', 'no'}:
                await self.send(query.message, self.store.decide(parts[2], owner, parts[3] == 'yes'))
            else:
                raise SafeError('Неизвестная кнопка. Откройте /ram.')
        except SafeError as exc:
            await self.send(query.message, str(exc))

    async def prepare(self, message, owner, role, job):
        cabinet = self.store.selected(owner)
        previous = self.store.claim(job, owner, cabinet, 'draft:' + role)
        if previous is not None:
            await self.send(message, previous)
            return
        ident = self.store.draft(owner, cabinet, role)
        buttons = types.InlineKeyboardMarkup()
        buttons.row(types.InlineKeyboardButton('Согласовать только план', callback_data=f'ram:decide:{ident}:yes'),
                    types.InlineKeyboardButton('Отклонить', callback_data=f'ram:decide:{ident}:no'))
        text = (f'Черновик {ident} · кабинет {cabinet}\n{ROLES[role]}\n'
                        'План: собрать исходные данные → подготовить предложение → проверить контролёром → '
                        'показать владельцу точные изменения.\n'
                        'В этой версии план не исполняется; никаких изменений WB. Согласование действует 15 минут.')
        self.store.finish(job, owner, cabinet, 'draft:' + role, 'prepared', text)
        await self.send(message, text, buttons)

    async def task(self, message):
        if not authorized(message):
            await message.reply('Доступ закрыт.')
            return
        text = message.get_args().strip().lower()
        # No LLM guesses about operations, arbitrary URLs or account selection.
        actions = {'продажи': 'sales', 'остатки': 'stocks'}
        roles = {'карточки': 'cards', 'реклама': 'ads', 'контент': 'content', 'финансы': 'finance'}
        try:
            if text in roles:
                await self.prepare(message, message.from_user.id, roles[text], f'message:{message.chat.id}:{message.message_id}')
            elif text in actions:
                owner = message.from_user.id
                cabinet = self.store.selected(owner)
                key = f'message:{message.chat.id}:{message.message_id}'
                await self.send(message, await self.chief.run(key, owner, cabinet, actions[text]), menu())
            else:
                await self.send(message, 'Доступны /task продажи и /task остатки. Другие роли — в меню /ram → Агенты. '
                                'Свободное планирование задач пока не подключено.')
        except SafeError as exc:
            await self.send(message, str(exc))

    def register(self, dp):
        dp.middleware.setup(OwnerMiddleware())
        dp.register_message_handler(self.open, commands=['ram'])
        dp.register_message_handler(self.open, lambda m: m.text == '🤖 RAM AI')
        dp.register_message_handler(self.task, commands=['task'])
        dp.register_callback_query_handler(self.callback, lambda q: (q.data or '').startswith('ram:'))
