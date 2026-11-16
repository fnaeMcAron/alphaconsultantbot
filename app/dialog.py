import time
from dataclasses import dataclass, field

TTL_SECONDS = 600

@dataclass
class Pending:
    question: str
    options: list = field(default_factory=list)
    ts: float = 0.0
    kind: str = "clarify"               # clarify уточнение вопроса, profile вопрос о бизнесе
    field: str = ""

class PendingStore:
    def __init__(self, ttl: float = TTL_SECONDS):
        self.ttl = ttl
        self._data = {}

    def put(self, chat_id: int, question: str, options: list, now: float = None,
            kind: str = "clarify", field: str = "") -> None:
        self._data[chat_id] = Pending(question, list(options), time.time() if now is None else now, kind, field)

    def peek(self, chat_id: int, now: float = None):
        p = self._data.get(chat_id)
        if p is None:
            return None
        if (time.time() if now is None else now) - p.ts > self.ttl:
            self._data.pop(chat_id, None)
            return None
        return p

    def pop(self, chat_id: int, now: float = None):
        p = self.peek(chat_id, now)
        self._data.pop(chat_id, None)
        return p


def looks_like_reply(text: str) -> bool:
    return len(text.split()) <= 8 and "?" not in text


class LastQuestion:
    def __init__(self, ttl: float = TTL_SECONDS):
        self.ttl = ttl
        self._data = {}

    def put(self, chat_id: int, question: str, now: float = None) -> None:
        self._data[chat_id] = (question, time.time() if now is None else now)

    def get(self, chat_id: int, now: float = None) -> str:
        q, ts = self._data.get(chat_id, ("", 0.0))
        if q and (time.time() if now is None else now) - ts > self.ttl:
            self._data.pop(chat_id, None)
            return ""
        return q

    def clear(self, chat_id: int) -> None:
        self._data.pop(chat_id, None)
