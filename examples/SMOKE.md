# Real-signup Smoke

Это не unit-тест. Это пошаговая ручная проверка SDK + relay + plugin против реального сервиса (Reddit / GitHub).

## Pre-requisites

1. Provider онлайн с известным `SCHEDULE_ID` на dev relay (`wss://relay.ittribe.org/ws/agent`)
2. Agent токен с ability `browser:relay` от dev backend
3. IMAP-доступ к `kom@ceki.me` (plus-addressing)
4. Chrome с установленным расширением `ceki-browser-extension` (dev build) на стороне провайдера
5. Provider положительный баланс (`agent:deposit` сделан)

## Env

```
export CEKI_API_KEY=1|<sanctum-token>
export CEKI_RELAY_URL=wss://relay.ittribe.org/ws/agent
export CEKI_ENV=dev
export SCHEDULE_ID=42
export IMAP_HOST=mail.ceki.me
export IMAP_USER=kom@ceki.me
export IMAP_PASS=<password>
```

## Reddit

```bash
EMAIL_TAG=browserlend-reddit-$(date +%s) python examples/reddit_signup.py
```

Чек-лист на провайдере:
- [ ] Side-panel чата открылся
- [ ] Пришёл скриншот капчи
- [ ] Провайдер вписал ответ — агент его получил
- [ ] Сессия не упала по heartbeat (-1011)
- [ ] Биллинг тикает (см. логи backend, agent_wallet -)

Чек-лист на агенте:
- [ ] connect успешен (handshake без 401)
- [ ] rent вернул match с chat_topic_id
- [ ] Page.navigate отрабатывает, loadEventFired ловится
- [ ] confirm-link получен из IMAP < 2 мин после сабмита
- [ ] Финальная страница «Email verified»

## GitHub

```bash
EMAIL_TAG=browserlend-github-$(date +%s) python examples/github_signup.py
```

## Известные риски

- Reddit Cloudflare: возможен soft-block если IP сильно подозрительный → провайдер должен иметь чистый residential IP
- GitHub puzzle: 2-3 раунда — провайдеру может надоесть, ставить таймауты и логировать
- IMAP rate limit: poll каждые 5с, не чаще

## Если smoke упал

- НЕ переключаться на example.com / synthetic (см. feedback_no_synthetic_smokes)
- Копать root cause: relay logs (`docker logs browser-relay`), backend logs, plugin chat-logger в Mongo
- Открыть issue с repro и логами
