import json
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from server import MAX_REPLY_CHARS, Skill, make_handler, to_speech


def alice(text, new=False, app="device-1", skill_id="skill-1"):
    return {
        "version": "1.0",
        "session": {
            "new": new,
            "session_id": "s-1",
            "skill_id": skill_id,
            "application": {"application_id": app},
            "user": {"user_id": "user-1"},
        },
        "request": {"original_utterance": text, "command": text.lower()},
    }


def text_of(response):
    return response["response"]["text"]


class SkillTest(unittest.TestCase):
    def test_fast_answer_comes_back_in_the_same_turn(self):
        skill = Skill(ask=lambda messages: "Париж.")
        self.assertEqual(text_of(skill.handle(alice("Столица Франции?"))), "Париж.")

    def test_slow_answer_is_delivered_on_the_next_turn(self):
        release = threading.Event()

        def slow(messages):
            release.wait(5)
            return "Долгий ответ."

        skill = Skill(ask=slow, reply_budget=0.05)
        first = skill.handle(alice("Расскажи про чёрные дыры"))
        self.assertIn("дайте подумать", text_of(first))
        self.assertEqual(first["response"]["buttons"][0]["title"], "Ну что?")

        self.assertIn("Ещё думаю", text_of(skill.handle(alice("ну что"))))
        release.set()
        time.sleep(0.05)
        self.assertEqual(text_of(skill.handle(alice("ну что"))), "Долгий ответ.")

    def test_history_is_sent_to_claude(self):
        seen = []
        skill = Skill(ask=lambda messages: (seen.append(messages), f"ответ {len(seen)}")[1])
        skill.handle(alice("первый"))
        skill.handle(alice("второй"))
        self.assertEqual(
            seen[1],
            [
                {"role": "user", "content": "первый"},
                {"role": "assistant", "content": "ответ 1"},
                {"role": "user", "content": "второй"},
            ],
        )

    def test_history_is_trimmed(self):
        seen = []
        skill = Skill(ask=lambda messages: (seen.append(messages), "ок")[1], history_turns=2)
        for i in range(5):
            skill.handle(alice(f"вопрос {i}"))
        self.assertEqual(len(seen[-1]), 5)
        self.assertEqual(seen[-1][0]["content"], "вопрос 2")

    def test_reset_and_stop(self):
        seen = []
        skill = Skill(ask=lambda messages: (seen.append(messages), "ок")[1])
        skill.handle(alice("запомни слово кит"))
        self.assertIn("забыла", text_of(skill.handle(alice("Забудь всё!"))))
        skill.handle(alice("какое слово"))
        self.assertEqual(len(seen[-1]), 1)
        self.assertTrue(skill.handle(alice("Хватит"))["response"]["end_session"])

    def test_greeting_ping_and_errors(self):
        def broken(messages):
            raise RuntimeError("network down")

        skill = Skill(ask=broken, name="Мия")
        self.assertIn("Мия", text_of(skill.handle(alice("", new=True))))
        self.assertEqual(text_of(skill.handle(alice("ping"))), "pong")
        self.assertIn("Не получилось", text_of(skill.handle(alice("привет"))))

    def test_private_skill_rejects_strangers(self):
        skill = Skill(ask=lambda messages: "ок", skill_id="skill-1", user_ids=["user-1"])
        self.assertEqual(text_of(skill.handle(alice("привет"))), "ок")
        self.assertTrue(skill.handle(alice("привет", skill_id="other"))["response"]["end_session"])

    def test_to_speech_strips_markdown_and_fits_limit(self):
        self.assertEqual(to_speech("# Заголовок\n- **жирно** и `код`"), "Заголовок жирно и код")
        long = "Предложение номер раз. " * 100
        spoken = to_speech(long)
        self.assertLessEqual(len(spoken), MAX_REPLY_CHARS)
        self.assertTrue(spoken.endswith("."))


class HttpTest(unittest.TestCase):
    def test_webhook_round_trip(self):
        skill = Skill(ask=lambda messages: "Привет из Claude.")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(skill, "/alice"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/alice"
            req = urllib.request.Request(url, data=json.dumps(alice("привет")).encode(), method="POST")
            with urllib.request.urlopen(req) as resp:
                body = json.loads(resp.read())
            self.assertEqual(body["version"], "1.0")
            self.assertEqual(body["response"]["tts"], "Привет из Claude.")
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
