import re
from dataclasses import dataclass, field
from typing import Optional

from .config import Settings
from .kb.search import Candidate, KnowledgeBase
from .kb.text import query_tokens
from .links import material_url
from .profile import FIELD_TITLES, LABELS, Profile, boost_terms, detect_regime, regime_family

EXPLICIT_RE = re.compile(
    r"посоветуй\w*|порекомендуй\w*|рекомендуй\w*|подбери\w*|подборк\w+|"
    r"что (почитать|изучить|посмотреть|послушать)|где почитать|какие материалы|"
    r"есть ли (у вас )?(курс|шаблон|калькулятор|документ|вебинар|подкаст|материал)\w*|"
    r"(нужен|нужна|нужно|нужны) (курс|шаблон|калькулятор|материал)\w*|с чего начать (обучение|изучение)")
_FILLER = re.compile(r"посоветуй\w*|порекомендуй\w*|рекомендуй\w*|подбери\w*|подборк\w+|материал\w*|почитать|изучить|"
                     r"посмотреть|послушать|есть ли|у вас|нужен|нужна|нужно|нужны|пожалуйста|мне|какие|что|"
                     r"с чего начать|обучение|изучение|по теме|на тему|курс\w*|шаблон\w*|образец|образцы|бланк\w*|калькулятор\w*|"
                     r"вебинар\w*|подкаст\w*|документ\w*")

IMPLICIT_MIN_CARD = 0.5

KIND_LABEL = {"article": "Статья", "document": "Документ", "calculator": "Калькулятор", "course": "Курс",
              "webinar": "Вебинар", "podcast": "Подкаст"}
KIND_WHY = {"article": "короткий разбор темы", "document": "готовый документ, можно сразу применить",
            "calculator": "калькулятор для расчётов", "webinar": "вебинар с экспертами", "podcast": "подкаст"}
INTENT_KINDS = [
    (re.compile(r"шаблон|образец|бланк|договор|документ"), {"document"}),
    (re.compile(r"калькулятор|рассчита|расчет"), {"calculator"}),
    (re.compile(r"научи|обучен|курс|с нуля|разобраться|пошагов|системно"), {"course"}),
    (re.compile(r"вебинар|эфир"), {"webinar"}),
    (re.compile(r"подкаст|послушать"), {"podcast"}),
]
_CANON = str.maketrans("саоерху", "caoepxy")
_canon = lambda s: s.lower().translate(_CANON)
SPHERE_INDUSTRIES = {
    "food": {"horeca"}, "retail": {"розница", "оптовая торговля"}, "marketplaces": {"ecom"},
    "services": {"услуги"}, "construction": {"строительство и недвижимость"}, "production": {"производство"},
}


@dataclass
class RecBlock:
    text: str = ""
    items: list = field(default_factory=list)
    paid: bool = False
    needs_topic: bool = False


def is_recommend_request(text: str) -> bool:
    return bool(EXPLICIT_RE.search(text.lower().replace("ё", "е")))


def topic_of(text: str) -> str:
    return _FILLER.sub(" ", text.lower().replace("ё", "е")).strip()


def _intent_kinds(text: str) -> set:
    t = text.lower().replace("ё", "е")
    kinds = set()
    for rx, ks in INTENT_KINDS:
        if rx.search(t):
            kinds |= ks
    return kinds


def _short(desc: str, n: int = 150) -> str:
    one = " ".join(desc.split())
    if len(one) <= n:
        return one
    cut = one[:n].rsplit(" ", 1)[0].rstrip(",;:—-")
    return cut + "…"


def _score(c: Candidate, prof: Profile, kinds: set) -> float:
    known, s = prof.known(), c.coverage + 0.5 * c.card_coverage
    if c.kind in kinds:
        s += 0.2
    sphere_ind = SPHERE_INDUSTRIES.get(known.get("sphere", ""), set())
    inds = {_canon(i) for i in c.industries}
    if sphere_ind & inds:
        s += 0.15
    if known.get("stage") in ("idea", "starting") and ("без опыта" in inds or c.kind == "course"):
        s += 0.1
    if known.get("stage") in ("running", "growing") and "с опытом" in inds:
        s += 0.1
    return s


def _reason(c: Candidate, prof: Profile) -> str:
    known, parts = prof.known(), []
    if c.kind == "course":
        n = c.meta.get("lessons")
        parts.append(f"пошаговый курс, {n} уроков" if n else "пошаговый курс")
    else:
        parts.append(KIND_WHY.get(c.kind, "материал по теме"))
    inds = {_canon(i) for i in c.industries}
    sphere = known.get("sphere")
    if sphere and SPHERE_INDUSTRIES.get(sphere, set()) & inds:
        parts.append(f"для сферы «{LABELS['sphere'][sphere]}»")
    reg = detect_regime(c.title)
    if reg and known.get("tax_regime") and regime_family(reg) == regime_family(known["tax_regime"]):
        parts.append(f"для вашего режима ({LABELS['tax_regime'][known['tax_regime']]})")
    if known.get("stage") in ("idea", "starting") and "без опыта" in inds:
        parts.append("подходит, если вы только начинаете")
    return "; ".join(parts)


def _line(cfg: Settings, c: Candidate, prof: Profile) -> str:
    link = material_url(cfg, c.kind, c.url, "recommend")
    if c.access == "free":
        head = f"• {KIND_LABEL.get(c.kind, 'Материал')}: {c.title}\n  {_reason(c, prof)}"
    else:
        price = (f"платный курс, {format(c.cost, ',').replace(',', ' ')} ₽" if c.access == "paid" and c.cost
                 else "курс по подписке «Курс+»")
        lessons = c.meta.get("lessons")
        head = f"• {c.title} ({price}{f', {lessons} уроков' if lessons else ''})"
        if c.description:
            head += f"\n  {_short(c.description)}"
    return head + (f"\n  {link}" if link else "")


def recommend(kb: KnowledgeBase, cfg: Settings, prof: Profile, question: str, *, exclude=(), explicit: bool = False
              ) -> Optional[RecBlock]:
    topic = topic_of(question) if explicit else question
    known = prof.known()
    if explicit and not query_tokens(topic):
        topic = " ".join(x for x in (LABELS["sphere"].get(known.get("sphere", ""), ""),
                                     boost_terms(known, "налоги")) if x)
        if not query_tokens(topic):
            return RecBlock(needs_topic=True)
    kinds = _intent_kinds(question)
    cands = kb.catalog_candidates(topic, extra=boost_terms(known, question))
    asked_regime = detect_regime(question) is not None
    user_family = regime_family(known.get("tax_regime"))
    skip = set(exclude) | (set() if explicit else set(prof.shown))
    good = []
    for c in cands:
        if (c.coverage if explicit else c.card_coverage) < (cfg.min_coverage if explicit else IMPLICIT_MIN_CARD) \
                or c.item_id in skip:
            continue
        item_regime = regime_family(detect_regime(c.title))
        if item_regime and user_family and item_regime != user_family and not asked_regime:
            continue
        good.append((_score(c, prof, kinds), c))
    good.sort(key=lambda x: -x[0])
    free = [c for _, c in good if c.access == "free"][: 3 if explicit else 2]
    paid_allowed = explicit or (prof.msgs >= 2 and (not prof.last_paid or prof.msgs - prof.last_paid >= 3))
    paid = next((c for _, c in good if c.access != "free"), None) if paid_allowed else None
    if not free and not paid:
        return RecBlock()
    parts = []
    if free:
        parts.append("💡 Что ещё может пригодиться:\n" + "\n".join(_line(cfg, c, prof) for c in free))
    if paid:
        parts.append("🔒 Если хотите разобраться глубже:\n" + _line(cfg, paid, prof))
        prof.last_paid = prof.msgs
    ids = [c.item_id for c in free] + ([paid.item_id] if paid else [])
    prof.shown = (prof.shown + ids)[-60:]
    return RecBlock("\n\n".join(parts), ids, paid is not None)
