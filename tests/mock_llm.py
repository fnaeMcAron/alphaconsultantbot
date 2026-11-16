"""Имитация OpenAI-совместимого сервера для проверки без реальной модели."""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIM = 256
LAST = {}  # последний запрос к чату (для проверок промпта)
CALLS = []  # какие запросы к чату получил сервер: 'rewrite' / 'answer'


def fake_embedding(text: str) -> list:
    v = [0.0] * DIM
    for w in text.lower().split():
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
    return v


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        LAST["body"] = body
        if self.path.endswith("/embeddings"):
            inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
            out = {"data": [{"index": i, "embedding": fake_embedding(t)} for i, t in enumerate(inputs)]}
        else:
            user = body["messages"][-1]["content"]
            system = body["messages"][0]["content"]
            if "переписываешь" in system:
                CALLS.append("rewrite")
                reply = user.split("Новая реплика:")[1].replace("/no_think", "").strip()
                if "ГАРБАЖ" in reply:
                    text = "я" * 300
                elif "юридическому лицу" in reply:
                    text = "Может ли юридическое лицо открыть ИП?"
                else:
                    text = reply
                out = {"choices": [{"message": {"content": text}}]}
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            CALLS.append("answer")
            if "подскажите" in user.split("Фрагменты")[0].lower() and "УТОЧНИТЬ:" in system:
                text = "УТОЧНИТЬ: Какой у вас режим налогообложения"
            elif "борщ" in user.split("Фрагменты")[0].lower():
                text = "НЕТ_ОТВЕТА"
            else:
                text = "<think>рассуждаю</think>Согласно материалу, ответ есть в источнике [1]."
            out = {"choices": [{"message": {"content": text}}]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"
