# Проверка MVP

2026-10-10: Python 3.11.17, исходная база main 7e14694.

- 51 offline unittest — PASS. В тестах запрещены socket.connect/getaddrinfo;
  HTTP ответы WB и OpenAI имитируются MockTransport, Telegram updates искусственные.
- uv pip check — 30 установленных пакетов совместимы.
- compileall и git diff --check — PASS.
- Production polling, Railway deploy, API-ключи владельца, реальные WB данные,
  реальный ответ доступной модели OpenAI — не проверялись и не запускались.

Покрытие: исходные кнопки, форматы заявок/PDF/URL, Telegram commands и callback,
owner-only/private-chat access, пустой список владельцев, выбор кабинета,
изоляция ключей/cache/journal, safe allowlist GET без редиректов, права/нет ключа,
неверные числа/даты/формат, возвраты и период, предупреждение неполных данных,
401/403/404/429/500/timeout/invalid JSON, persisted budget/rate/cooldown/dedup,
конкурентный duplicate job, подтверждение владельцем/TTL/повтор/отказ,
недоступная/неуказанная модель, отсутствие повторного платного вызова и
повторной отправки Apps Script, отсутствие секретов в ошибках/журнале.

В ограниченном cloud sandbox без сетевых системных вызовов CPython зависал
на shutdown_default_executor даже в минимальном локальном asyncio.to_thread.
Тесты выполнены с разрешёнными сетевыми системными вызовами, при этом сами тесты
сохраняли запрет внешних соединений через unittest.mock. Боевых запросов не было.

Staging-приёмка и необходимые Variables описаны в README. CI тестирует PR без
секретов; workflow не развёртывает сервис. Пока staging не пройден, нельзя считать
404 подтверждённо устранённым в production или доступ WB подтверждённым.
