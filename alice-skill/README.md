# Claude в Яндекс Станции

Навык Алисы, который пересылает ваши вопросы в Claude и зачитывает ответы.

```
«Алиса, запусти навык Claude» → Яндекс Диалоги → этот сервер → Claude API → голос Алисы
```

Что умеет:

- **Помнит разговор.** Последние 8 пар «вопрос — ответ» на каждом устройстве уходят в Claude как контекст. После 30 минут тишины память сбрасывается.
- **Не падает по таймауту.** Алиса ждёт ответ около трёх секунд. Если Claude не успел, навык скажет «дайте подумать» и покажет кнопку «Ну что?». Ответ продолжает готовиться, и на следующую реплику («ну что») Алиса его зачитает.
- **Говорит, а не пишет.** Просит Claude отвечать коротко и без разметки, вычищает оставшиеся `**`, `#` и списки, укладывает ответ в лимит Алисы (1024 символа).
- **Понимает команды:** «забудь всё» или «начни сначала» стирает память, «хватит» или «стоп» закрывает навык.
- **Может быть приватным.** Принимает запросы только от вашего навыка и вашего аккаунта.

## Что понадобится

1. **Ключ Claude API** с console.anthropic.com. API недоступен из России, поэтому сервер должен стоять за рубежом.
2. **VPS за рубежом** с Python 3.10+ и открытыми портами 80 и 443.
3. **Домен** (подойдёт и бесплатный поддомен), A-запись которого указывает на IP сервера. Алиса работает только по HTTPS.

## Установка на сервер

```bash
sudo mkdir -p /opt/alice-claude && cd /opt/alice-claude
sudo cp /путь/к/asterdroid/alice-skill/server.py .
sudo python3 -m venv venv
sudo venv/bin/pip install -r /путь/к/asterdroid/alice-skill/requirements.txt

sudo cp /путь/к/asterdroid/alice-skill/alice-claude.env.example /etc/alice-claude.env
sudo chmod 600 /etc/alice-claude.env
sudo nano /etc/alice-claude.env      # вставьте ключ и придумайте секретный ALICE_PATH

sudo cp /путь/к/asterdroid/alice-skill/alice-claude.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alice-claude
```

HTTPS проще всего сделать через [Caddy](https://caddyserver.com): он сам получит сертификат.

```bash
sudo apt install caddy
sudo cp /путь/к/asterdroid/alice-skill/Caddyfile.example /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile       # впишите свой домен
sudo systemctl reload caddy
```

Проверка: `curl https://ваш-домен/` должен ответить `{"status": "ok"}`.

## Создание навыка в Яндексе

1. Откройте [dialogs.yandex.ru/developer](https://dialogs.yandex.ru/developer) → «Создать диалог» → «Навык».
2. **Название и имя активации:** например, «Claude». Именно его вы будете говорить: «Алиса, запусти навык Claude». Яндекс иногда не пропускает имена-бренды; тогда возьмите своё, например «Мия», и задайте то же в `ASSISTANT_NAME`.
3. **Backend → Webhook URL:** `https://ваш-домен` + ваш `ALICE_PATH`, например `https://alice.example.com/alice/x7k2p9`.
4. **Доступ: «Приватный».** Навык будет работать только в вашем аккаунте и не требует модерации.
5. Сохраните и нажмите «Опубликовать». Затем на вкладке «Тестирование» напишите «привет».

## Делаем навык закрытым

Webhook виден только Яндексу, но лучше подстраховаться:

1. Запустите навык один раз и посмотрите журнал: `journalctl -u alice-claude -n 20`. Там будет строка `new session: skill_id=... user_id=...`.
2. Впишите эти значения в `ALICE_SKILL_ID` и `ALICE_USER_IDS` (несколько пользователей перечисляются через запятую) в `/etc/alice-claude.env`.
3. `sudo systemctl restart alice-claude`.

Теперь запросы от чужих навыков и аккаунтов получают вежливый отказ, а Claude API не тратится.

## Настройки

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Ключ Claude API |
| `ALICE_PATH` | `/alice` | Путь webhook'а; сделайте его длинным и случайным |
| `ALICE_SKILL_ID` | пусто | Принимать запросы только от этого навыка |
| `ALICE_USER_IDS` | пусто | Принимать запросы только от этих пользователей |
| `ASSISTANT_NAME` | `Claude` | Как ассистент представляется |
| `SYSTEM_PROMPT` | короткий голосовой промпт | Свой характер и правила для Claude |
| `CLAUDE_MODEL` | `claude-haiku-5-5` | Модель. Haiku самая быстрая; `claude-sonnet-5-5` или `claude-opus-5-5` умнее, но чаще будут уходить в «дайте подумать» |
| `CLAUDE_EFFORT` | `low` | Сколько модель думает: `low`, `medium`, `high` |
| `CLAUDE_MAX_TOKENS` | `400` | Предел длины ответа |
| `REPLY_BUDGET` | `2.4` | Сколько секунд ждать Claude, прежде чем сказать «дайте подумать» |
| `HISTORY_TURNS` | `8` | Сколько пар реплик помнить |
| `SESSION_TTL` | `1800` | Через сколько секунд тишины забывать разговор |
| `HOST`, `PORT` | `127.0.0.1`, `8080` | Где слушает сервер (снаружи его закрывает Caddy) |

## Проверка без станции

```bash
cd alice-skill
python3 -m unittest -v          # тесты, ключ не нужен

ANTHROPIC_API_KEY=sk-ant-... python3 server.py &
curl -s localhost:8080/alice -d '{"version":"1.0","session":{"new":true},"request":{"original_utterance":"Почему небо голубое?"}}'
```

## Сколько это стоит

Haiku 5.5 стоит $0.10 за миллион входных токенов и $0.50 за миллион выходных. Обычный голосовой вопрос с историей разговора обходится в сотые доли цента, так что сотня вопросов в день почти ничего не стоит. Основные расходы — это VPS.
