#!/usr/bin/env python3
"""Навык Алисы, который отвечает голосом Claude.

Яндекс Диалоги шлют сюда POST с распознанной фразой, сервер спрашивает
Claude и возвращает текст, который Алиса зачитывает. Алиса ждёт ответ
около трёх секунд, поэтому долгий ответ не теряется: навык говорит
«дайте подумать», а готовый текст отдаёт на следующей реплике.

Зависимость одна: пакет anthropic. Настройки берутся из окружения,
полный список в README.md.
"""

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("alice-claude")

# Алиса не зачитывает больше 1024 символов ни в text, ни в tts.
MAX_REPLY_CHARS = 1024

STOP_COMMANDS = {"стоп", "хватит", "выход", "выйти", "закончить", "закончим", "пока", "до свидания"}
RESET_COMMANDS = {"забудь", "забудь все", "новый разговор", "начни сначала", "начнем сначала", "сначала"}
CHECK_COMMANDS = {"ну что", "ну", "ну как", "готово", "что там", "ответ", "дальше", "и что", "ты тут"}

CHECK_BUTTON = [{"title": "Ну что?", "hide": True}]


def env(name, default):
    value = os.environ.get(name, "").strip()
    return value if value else default


def normalize(text):
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def to_speech(text):
    """Убирает разметку, которую голос всё равно не прочтёт, и укладывает текст в лимит."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[*_#`>|~]", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    text = " ".join(text.split())
    if len(text) <= MAX_REPLY_CHARS:
        return text
    cut = text[:MAX_REPLY_CHARS]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > MAX_REPLY_CHARS // 2 else cut[: MAX_REPLY_CHARS - 1] + "…"


def make_claude_ask():
    """Возвращает функцию messages -> текст ответа, которая ходит в Claude API."""
    import anthropic

    client = anthropic.Anthropic(timeout=float(env("CLAUDE_TIMEOUT", "30")), max_retries=1)
    model = env("CLAUDE_MODEL", "claude-haiku-5-5")
    effort = env("CLAUDE_EFFORT", "low")
    max_tokens = int(env("CLAUDE_MAX_TOKENS", "400"))
    system = env("SYSTEM_PROMPT", default_system_prompt(env("ASSISTANT_NAME", "Claude")))

    def ask(messages):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            output_config={"effort": effort},
            messages=messages,
        )
        if response.stop_reason == "refusal":
            return "На эту тему я ответить не могу. Давайте о чём-нибудь другом."
        return "".join(block.text for block in response.content if block.type == "text").strip()

    return ask


def default_system_prompt(name):
    return (
        f"Тебя зовут {name}. Ты голосовой ассистент на колонке Яндекс Станция. "
        "Твои ответы зачитывает синтезатор речи, поэтому говори по-русски, живо и коротко: "
        "обычно два-четыре предложения. Никакой разметки, списков, таблиц, эмодзи и ссылок. "
        "Числа и сокращения пиши так, как их удобно произнести вслух. "
        "Если вопрос требует длинного ответа, дай суть и предложи рассказать подробнее."
    )


@dataclass
class Conversation:
    history: list = field(default_factory=list)
    pending: Future = None
    last_used: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)


class Skill:
    def __init__(
        self,
        ask,
        reply_budget=2.4,
        history_turns=8,
        session_ttl=1800,
        skill_id=None,
        user_ids=(),
        name="Claude",
    ):
        self.ask = ask
        self.reply_budget = reply_budget
        self.history_turns = history_turns
        self.session_ttl = session_ttl
        self.skill_id = skill_id
        self.user_ids = set(user_ids)
        self.name = name
        self.executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="claude")
        self.conversations = {}
        self.lock = threading.Lock()

    def handle(self, body):
        started = time.monotonic()
        version = body.get("version", "1.0")
        session = body.get("session") or {}
        request = body.get("request") or {}

        def reply(text, end=False, buttons=None):
            text = to_speech(text)
            response = {"text": text, "tts": text, "end_session": end}
            if buttons:
                response["buttons"] = buttons
            return {"version": version, "response": response}

        utterance = (request.get("original_utterance") or "").strip()
        command = normalize(request.get("command") or utterance)

        # Яндекс периодически проверяет, жив ли навык.
        if command == "ping":
            return reply("pong")
        if session.get("new"):
            log.info(
                "new session: skill_id=%s user_id=%s",
                session.get("skill_id"),
                (session.get("user") or {}).get("user_id") or (session.get("application") or {}).get("application_id"),
            )
        if not self.allowed(session):
            return reply("Это приватный навык, он работает только у хозяина.", end=True)

        conv = self.conversation(session)

        if command in STOP_COMMANDS:
            return reply("До связи! Зовите, если что.", end=True)
        if command in RESET_COMMANDS:
            with conv.lock:
                conv.history.clear()
                conv.pending = None
            return reply("Всё забыла. Начнём с чистого листа.")

        if conv.pending is not None:
            return self.deliver(conv, command, started, reply)

        if not utterance:
            if session.get("new"):
                return reply(f"Привет! Это {self.name}. Спрашивайте что угодно.")
            return reply("Я слушаю.")
        if command in CHECK_COMMANDS:
            return reply("Все ответы уже у вас. Спрашивайте дальше.")

        with conv.lock:
            history = list(conv.history)
        conv.pending = self.executor.submit(self.think, conv, history, utterance)
        return self.deliver(conv, None, started, reply)

    def deliver(self, conv, command, started, reply):
        """Отдаёт готовый ответ или просит подождать, укладываясь в таймаут Алисы."""
        pending = conv.pending
        wait([pending], timeout=max(0.0, self.reply_budget - (time.monotonic() - started)))
        if not pending.done():
            if command is None:
                return reply("Хороший вопрос, дайте подумать. Скажите «ну что», и я отвечу.", buttons=CHECK_BUTTON)
            return reply("Ещё думаю. Спросите «ну что» через пару секунд.", buttons=CHECK_BUTTON)
        conv.pending = None
        answer = pending.result()
        if command and command not in CHECK_COMMANDS:
            answer += " А новый вопрос повторите, пожалуйста."
        return reply(answer)

    def think(self, conv, history, utterance):
        messages = history + [{"role": "user", "content": utterance}]
        try:
            answer = self.ask(messages) or "Мне нечего на это сказать."
        except Exception:
            log.exception("Claude request failed")
            return "Не получилось достучаться до Claude. Попробуйте ещё раз чуть позже."
        with conv.lock:
            conv.history += [{"role": "user", "content": utterance}, {"role": "assistant", "content": answer}]
            del conv.history[: -2 * self.history_turns]
        return answer

    def allowed(self, session):
        if self.skill_id and session.get("skill_id") != self.skill_id:
            return False
        if self.user_ids:
            user_id = (session.get("user") or {}).get("user_id") or (session.get("application") or {}).get("application_id")
            return user_id in self.user_ids
        return True

    def conversation(self, session):
        # Ключ по устройству, а не по сессии: короткий перерыв не стирает память.
        key = (session.get("application") or {}).get("application_id") or session.get("session_id") or "anonymous"
        now = time.monotonic()
        with self.lock:
            for stale in [k for k, c in self.conversations.items() if now - c.last_used > self.session_ttl and c.pending is None]:
                del self.conversations[stale]
            conv = self.conversations.setdefault(key, Conversation())
            conv.last_used = now
            return conv


def make_handler(skill, path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_json(200, {"status": "ok"})

        def do_POST(self):
            if self.path.split("?")[0] != path:
                self.send_json(404, {"error": "not found"})
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                self.send_json(400, {"error": "bad json"})
                return
            self.send_json(200, skill.handle(body))

        def send_json(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    return Handler


def main():
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    name = env("ASSISTANT_NAME", "Claude")
    skill = Skill(
        ask=make_claude_ask(),
        reply_budget=float(env("REPLY_BUDGET", "2.4")),
        history_turns=int(env("HISTORY_TURNS", "8")),
        session_ttl=int(env("SESSION_TTL", "1800")),
        skill_id=env("ALICE_SKILL_ID", None),
        user_ids=[u.strip() for u in env("ALICE_USER_IDS", "").split(",") if u.strip()],
        name=name,
    )
    host, port, path = env("HOST", "127.0.0.1"), int(env("PORT", "8080")), env("ALICE_PATH", "/alice")
    server = ThreadingHTTPServer((host, port), make_handler(skill, path))
    log.info("Alice skill listening on http://%s:%d%s", host, port, path)
    server.serve_forever()


if __name__ == "__main__":
    main()
