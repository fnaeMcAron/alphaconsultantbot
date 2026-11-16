import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

UNKNOWN = "unknown"

LABELS = {
    "legal_form": {"ip": "ИП", "ooo": "ООО", "selfemployed": "самозанятый", "none": "бизнес пока не оформлен"},
    "tax_regime": {"usn_income": "УСН «Доходы»", "usn_income_expense": "УСН «Доходы минус расходы»", "usn": "УСН",
                   "patent": "патент", "ausn": "АУСН", "npd": "НПД (самозанятый)", "osno": "ОСНО", "eshn": "ЕСХН"},
    "stage": {"idea": "идея, бизнес ещё не открыт", "starting": "недавно открыт", "running": "работает давно",
              "growing": "расширяется"},
    "sphere": {"food": "общепит", "retail": "розничная торговля", "marketplaces": "маркетплейсы", "services": "услуги",
               "production": "производство", "it": "IT и онлайн", "construction": "строительство",
               "education": "образование"},
}
FIELD_TITLES = {"legal_form": "форма", "tax_regime": "налоговый режим", "stage": "этап", "sphere": "сфера",
                "marketplaces": "площадки"}

QUESTIONS = {
    "tax_regime": ("Чтобы подсказывать точнее, уточните: какой у вас режим налогообложения?",
                   [("УСН «Доходы»", "usn_income"), ("УСН «Доходы минус расходы»", "usn_income_expense"),
                    ("Патент", "patent"), ("АУСН", "ausn"), ("НПД (самозанятый)", "npd"), ("ОСНО", "osno"),
                    ("Пока не знаю", UNKNOWN)]),
    "sphere": ("Чтобы подсказывать точнее, уточните: в какой сфере ваш бизнес?",
               [("Общепит", "food"), ("Розница, магазин", "retail"), ("Маркетплейсы", "marketplaces"),
                ("Услуги", "services"), ("Производство", "production"), ("IT, онлайн", "it"), ("Другое", UNKNOWN)]),
    "stage": ("Чтобы подсказывать точнее, уточните: на каком этапе ваш бизнес?",
              [("Только планирую", "idea"), ("Недавно открыл(а)", "starting"), ("Работаю давно", "running"),
               ("Расширяюсь", "growing")]),
}

_NORM = lambda s: s.lower().replace("ё", "е")
_FIRST = re.compile(r"\b(я|мы|у меня|у нас|наш\w*|мо[йяеи]|работаю|работаем|открыл\w*|зарегистрировал\w*|веду|ведем|"
                    r"занимаюсь|занимаемся|продаю|продаем|планирую|собираюсь|хочу открыть|думаю открыть)\b")
_QWORDS = re.compile(r"\b(как|что|какой|какая|какие|какое|когда|сколько|почему|зачем|где|можно|нужно ли|стоит ли|"
                     r"подскажите|посоветуйте|расскажите|помогите)\b")
_DONT_KNOW = re.compile(r"не знаю|пока не|затрудняюсь|без понятия|не помню")


def _regime(t: str) -> Optional[str]:
    if re.search(r"\bаусн\b", t):
        return "ausn"
    if re.search(r"\bусн\b|упрощен\w*", t):
        if re.search(r"доходы\s*(минус|-|—|–)\s*расходы|15\s*%", t):
            return "usn_income_expense"
        if re.search(r"6\s*%|доходы", t):
            return "usn_income"
        return "usn"
    if re.search(r"патент\w*", t):
        return "patent"
    if re.search(r"\bнпд\b|самозанят\w*|налог\w* на профессиональн", t):
        return "npd"
    if re.search(r"\bосно\b|общ\w+ систем\w+", t):
        return "osno"
    if re.search(r"\bесхн\b", t):
        return "eshn"
    return None


def _legal(t: str) -> Optional[str]:
    if re.search(r"самозанят", t):
        return "selfemployed"
    if re.search(r"\bооо\b|общество с ограниченной", t):
        return "ooo"
    if re.search(r"\bип\b|индивидуальн\w* предпринимател", t):
        return "ip"
    if re.search(r"пока (нет|не оформ)|не оформ\w+|не зарегистрир\w+|только планирую|собираюсь открыть|хочу открыть", t):
        return "none"
    return None


def _stage(t: str) -> Optional[str]:
    if re.search(r"планирую открыть|хочу открыть|собираюсь открыть|только идея|думаю открыть", t):
        return "idea"
    if re.search(r"только (что )?открыл|недавно открыл|начинаю|только начал|начал недавно|первый год", t):
        return "starting"
    if re.search(r"масштабир|расширя|несколько точек|открываю втор", t):
        return "growing"
    if re.search(r"давно работа|несколько лет|уже \w+ лет|действующ", t):
        return "running"
    return None


_SPHERES = [
    ("food", r"кафе|ресторан|кофейн|пекарн|общепит|столов\w+|фастфуд|доставк\w+ еды"),
    ("marketplaces", r"маркетплейс|ozon|озон|wildberries|вайлдберриз|\bwb\b|\bвб\b|яндекс.?маркет"),
    ("retail", r"магазин|розниц|торгую|торговл"),
    ("services", r"услуг|салон|барбер|парикмахер|маникюр|клининг|автосервис"),
    ("production", r"производств|изготавлива|\bцех|мастерск"),
    ("it", r"\bit\b|разработ|приложени|\bсайт|стартап|онлайн"),
    ("construction", r"строител|ремонт квартир|отделк"),
    ("education", r"курсы|репетитор|обучени|школ\w+ для"),
]


def _sphere(t: str) -> Optional[str]:
    for code, rx in _SPHERES:
        if re.search(rx, t):
            return code
    return None


def detect_regime(text: str) -> Optional[str]:
    return _regime(_NORM(text))


def regime_family(code: Optional[str]) -> Optional[str]:
    return "usn" if code and code.startswith("usn") else code


_PARSERS = {"tax_regime": _regime, "legal_form": _legal, "stage": _stage, "sphere": _sphere}


def extract_profile(text: str) -> dict:
    t = _NORM(text)
    if "?" in t or not _FIRST.search(t):
        return {}
    out = {f: v for f, p in _PARSERS.items() if (v := p(t))}
    if out.get("tax_regime") == "npd":
        out["legal_form"] = "selfemployed"
    return out


def parse_field(field_name: str, text: str) -> Optional[str]:
    t = _NORM(text)
    if _DONT_KNOW.search(t):
        return UNKNOWN
    return _PARSERS[field_name](t) if field_name in _PARSERS else None


def is_statement_only(text: str) -> bool:
    t = _NORM(text)
    return "?" not in t and not _QWORDS.search(t) and len(t.split()) <= 14


_TAX_RE = re.compile(r"налог|усн|патент|ндс|взнос|отчетн|декларац|аусн|нпд|бухгалтер|страхов|вычет")
_SALES_RE = re.compile(r"продвиж|реклам|маркетинг|продаж|клиент|трафик|конверси")
_REGIME_WORDS = {
    "usn_income": "усн упрощенка", "usn_income_expense": "усн упрощенка", "usn": "усн упрощенка",
    "patent": "патент", "ausn": "аусн", "npd": "самозанятые нпд",
    "osno": "осно ндс налог на прибыль", "eshn": "есхн",
}


@dataclass
class Profile:
    fields: dict = field(default_factory=dict)
    asked: list = field(default_factory=list)
    msgs: int = 0
    last_asked: int = 0
    shown: list = field(default_factory=list)
    last_paid: int = 0
    recs: bool = True

    def known(self) -> dict:
        return {k: v for k, v in self.fields.items() if v and v != UNKNOWN}

    def summary(self) -> str:
        parts = []
        for f in ("legal_form", "tax_regime", "stage", "sphere"):
            v = self.known().get(f)
            if v:
                parts.append(f"{FIELD_TITLES[f]}: {LABELS[f].get(v, v)}")
        return "; ".join(parts)

    def set(self, field_name: str, value: str) -> None:
        self.fields[field_name] = value
        if field_name == "tax_regime" and value == "npd":
            self.fields.setdefault("legal_form", "selfemployed")


def next_question(p: Profile, question: str) -> Optional[str]:
    if p.last_asked and p.msgs - p.last_asked < 3:
        return None
    t = _NORM(question)
    for name, rx in (("tax_regime", _TAX_RE), ("sphere", _SALES_RE)):
        if name not in p.asked and not p.fields.get(name) and rx.search(t):
            return name
    if p.msgs >= 3 and "stage" not in p.asked and not p.fields.get("stage"):
        return "stage"
    return None


def boost_terms(fields: dict, question: str) -> str:
    regime = fields.get("tax_regime")
    t = _NORM(question)
    if not regime or regime == UNKNOWN or not _TAX_RE.search(t) or _regime(t):
        return ""
    return _REGIME_WORDS.get(regime, "")


class ProfileStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY, data TEXT, updated REAL)")

    def _db(self):
        return sqlite3.connect(self.path)

    def get(self, chat_id: int) -> Profile:
        db = self._db()
        try:
            row = db.execute("SELECT data FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        finally:
            db.close()
        if not row:
            return Profile()
        d = json.loads(row[0])
        return Profile(d.get("fields", {}), d.get("asked", []), d.get("msgs", 0), d.get("last_asked", 0),
                       d.get("shown", []), d.get("last_paid", 0), d.get("recs", True))

    def save(self, chat_id: int, p: Profile) -> None:
        data = json.dumps({"fields": p.fields, "asked": p.asked, "msgs": p.msgs, "last_asked": p.last_asked,
                           "shown": p.shown[-60:], "last_paid": p.last_paid, "recs": p.recs},
                          ensure_ascii=False)
        with self._db() as db:
            db.execute("INSERT INTO users (chat_id, data, updated) VALUES (?,?,?) "
                       "ON CONFLICT(chat_id) DO UPDATE SET data=excluded.data, updated=excluded.updated",
                       (chat_id, data, time.time()))

    def delete(self, chat_id: int) -> None:
        with self._db() as db:
            db.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
