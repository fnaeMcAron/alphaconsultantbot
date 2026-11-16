import hashlib
import json
import sqlite3
import time
from pathlib import Path


class Events:
    def __init__(self, path, salt: str = ""):
        self.path, self.salt = Path(path), salt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, user TEXT, type TEXT, payload TEXT)")

    def user_hash(self, chat_id: int) -> str:
        return hashlib.sha256(f"{self.salt}:{chat_id}".encode()).hexdigest()[:16]

    def log(self, chat_id: int, type_: str, **payload) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO events (ts, user, type, payload) VALUES (?,?,?,?)",
                       (time.time(), self.user_hash(chat_id), type_, json.dumps(payload, ensure_ascii=False)))
