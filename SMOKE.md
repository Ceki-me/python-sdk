# Smoke Test Runbook — browserlend MVP

После переключения relay на Sanctum introspect (#79) и фикса cmd-channel hang (#77).

> **Synthetic smokes удалены 2026-04-30.** Файлы `mvp_smoke.py`, `quickstart.py`, `login_flow.py` (example.com / app.example.com) удалены. Browserlend MVP smoke = real-site тест (Reddit/Twitter/GitHub) — см. отдельный smoke-runner у ceki-qa.

## Что проверяет smoke runner

- **connect** — agent WS подключается к relay (Sanctum introspect, type=agent, ability browser:relay)
- **session** — создание сессии (matchmaker, provider match, WebRTC P2P handshake)
- **navigate + query + click** — команды через ceki-cmd DataChannel (баг #77: cmd-channel не виснет после navigate)
- **screenshot** — получение скриншота через P2P
- **chat** — отправка/получение сообщений и изображений через relay chat
- **session end** — корректное завершение сессии

Скрипт: `examples/mvp_smoke_p2p.py` — relay chat + P2P commands (навигация на github.com)

## Pre-flight checklist

1. **browser-relay** запущен на dev из ветки `feature/browserlend` (коммит `0e1bdb7`+):
   ```bash
   docker compose logs browser-relay | grep "introspect"
   # должно быть: listening on 0.0.0.0:8443
   ```

2. **Chrome extension** собран dev-сборкой, распакован в Chrome у провайдера (Константин):
   - `chrome://extensions/` > Load unpacked > `dist-extension/dev/`
   - Залогиниться как provider, нажать "Go Online"

3. **Provider онлайн** — виден в relay-логах:
   ```bash
   docker compose logs -f browser-relay | grep "provider"
   # должно быть: [ws] provider connected connId=...
   ```

4. **Agent-токен сгенерирован** — Sanctum PAT с ability `browser:relay`:
   ```bash
   docker compose exec api php artisan tinker --execute="echo App\Models\User::find(1)->createToken('smoke-agent', ['browser:relay'])->plainTextToken;"
   ```
   Выведет токен вида `123|abc...xyz`. Это `CEKI_TOKEN`.

## Запуск

```bash
cd python-sdk
pip install -e .
CEKI_TOKEN="<токен из шага 4>" \
RELAY_URL="wss://browser.ittribe.org/ws/agent" \
python examples/mvp_smoke_p2p.py
```

## Что видеть в выводе (success)

```
INFO  mvp_smoke_p2p: PASS  connect — agent_id=...
INFO  mvp_smoke_p2p: PASS  session_matched — session_id=...
INFO  mvp_smoke_p2p: PASS  rtc_connected — connectionState=connected
INFO  mvp_smoke_p2p: PASS  chat_available — relay chat API ready
INFO  mvp_smoke_p2p: PASS  navigate — url=https://github.com
INFO  mvp_smoke_p2p: PASS  query_dom — text='...'
INFO  mvp_smoke_p2p: PASS  click — a
INFO  mvp_smoke_p2p: PASS  screenshot — 1920x1080 150KB
INFO  mvp_smoke_p2p: PASS  session_end — reason=completed
INFO  mvp_smoke_p2p: STATUS: PASS
```

Критические шаги (FAIL = smoke красный):
`connect`, `session_matched`, `rtc_connected`, `navigate`, `query_dom`, `screenshot`, `chat_send_text`, `chat_send_image`, `session_end`

## Мониторинг (два терминала)

**Терминал 1 — relay-логи:**
```bash
docker compose logs -f browser-relay
```
Искать:
- `[auth] agent introspect ok: user_id=1 subject_type=agent` — auth прошёл через новый introspect
- `[session] REQUESTED → MATCHING → OFFERED → STARTING → ACTIVE` — сессия создана
- `[revocation]` — если появится, значит Redis subscriber работает

**Терминал 2 — Chrome DevTools у провайдера:**
- `chrome://extensions/` > service worker > Inspect
- Консоль: `[ceki]` сообщения о подключении, offer/answer, session state

## Известные не-баги

- `type` step может быть `type_skipped` — на целевом сайте может не быть input-полей, это ожидаемо
- `chat_recv_message` может FAIL если провайдер не ответил вручную в течение 30с — не баг runner'а
- `chat_history_partial` — история может быть пустой если chat-service не сохранил сообщения, не критично
- В `session.test.ts` (js-sdk) 2 pre-existing failures — не связаны со smoke

## Критерий success/fail

| Результат | Что значит |
|-----------|-----------|
| `STATUS: PASS` | Все шаги прошли, smoke зелёный |
| `STATUS: PARTIAL PASS` | Некритические шаги (type, chat_recv) failed — OK для первого прогона |
| `STATUS: FAIL` | Критический шаг failed — нужна диагностика |

Если `connect` FAIL с `Auth failed: 401` — токен невалиден или relay ещё на старом коде (не introspect).
Если `session_matched` FAIL с timeout — провайдер не онлайн или extension не подключён к relay.
Если `navigate` FAIL с timeout — cmd-channel hang (баг #77), проверить что python-sdk на коммите `f8497ef`+.
