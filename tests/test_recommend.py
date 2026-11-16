"""Проверка рекомендаций: python -m tests.test_recommend  (нужен data/kb.db; вместо Ollama заглушка)."""
import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from tests import mock_llm
from tests.test_handlers import Msg


def unit(db_path: Path) -> None:
    from app.analytics import Events
    from app.config import load_settings
    from app.kb.search import KnowledgeBase
    from app.profile import Profile
    from app.recommend import is_recommend_request, recommend, topic_of

    # просьбы посоветовать отличаются от обычных вопросов
    for q in ("посоветуй курс по маркетингу", "что почитать про налоги", "есть ли шаблон договора аренды", "нужен калькулятор налогов"):
        assert is_recommend_request(q), q
    for q in ("Как платить налоги на УСН?", "как открыть ИП", "расскажи про маркетинг", "что такое НДС"):
        assert not is_recommend_request(q), q
    t = topic_of("посоветуй курс по маркетингу")
    assert "маркетинг" in t and "курс" not in t, t
    print("OK просьбы посоветовать распознаются, слова-типы не считаются темой")

    # КОНФИДЕНЦИАЛЬНОСТЬ: у платных материалов в индексе только публичная карточка
    db = sqlite3.connect(db_path)
    paid = db.execute("SELECT id, description FROM items WHERE access IN ('paid','subscription')").fetchall()
    assert paid, "в каталоге должны быть платные материалы"
    for iid, desc in paid:
        chunks = db.execute("SELECT text, answerable FROM chunks WHERE item_id=?", (iid,)).fetchall()
        assert len(chunks) <= 1 and all(c[0] == desc and c[1] == 0 for c in chunks), (iid, len(chunks))
    print(f"OK платные материалы ({len(paid)}): в индексе только карточка, для ответов не используются")

    cfg = load_settings()
    kb = KnowledgeBase(cfg.kb_path, cfg.min_coverage, cfg.min_dense)

    # явная просьба: платное предложение со ссылкой, без содержания
    p = Profile()
    b = recommend(kb, cfg, p, "посоветуй курс по маркетингу", explicit=True)
    assert b.paid and "Интернет-маркетинг" in b.text and "по подписке «Курс+»" in b.text
    paid_part = b.text.split("🔒")[1]
    assert len(paid_part) < 420 and "utm_campaign=recommend" in paid_part, paid_part
    print("OK явная просьба: платный курс описан кратко, со ссылкой")

    # цена платного курса форматируется аккуратно
    b = recommend(kb, cfg, Profile(), "посоветуй материалы по налогам на маркетплейсах", explicit=True)
    assert "платный курс, 3 990 ₽" in b.text, b.text
    print("OK цена платного курса:", "3 990 ₽")

    # платные предложения по ходу: не в первом ответе, не чаще раза в 3 ответа, один материал не повторяется
    q = "как продвигать товары на Ozon"
    first = recommend(kb, cfg, Profile(msgs=1), q)
    assert first and not first.paid, "в первых ответах платное не предлагаем"
    p = Profile(msgs=5)
    a = recommend(kb, cfg, p, q)
    assert a.paid and p.last_paid == 5
    p.msgs = 6
    b2 = recommend(kb, cfg, p, q)
    assert not (b2 and b2.paid), "после платного предложения пауза"
    p.msgs = 12
    b3 = recommend(kb, cfg, p, q)
    assert not (b3 and b3.paid), "уже показанное платное повторно не предлагаем"
    assert not set(a.items) & set(b3.items if b3 else []), "показанное не повторяется"
    print("OK ритм платных предложений: не в первом ответе, пауза 3 ответа, без повторов")

    # режим налогообложения: материалы для другого режима не предлагаем
    patent = Profile(fields={"tax_regime": "patent"}, msgs=5)
    b = recommend(kb, cfg, patent, "как платить налоги")
    assert b and "УСН" not in b.text and "АУСН" not in b.text, b.text
    b = recommend(kb, cfg, Profile(fields={"tax_regime": "patent"}, msgs=5), "как платить налоги на УСН")
    assert b and "УСН" in b.text, "если пользователь сам спросил про УСН, фильтр не мешает"
    print("OK режим из профиля: чужой режим не предлагаем, а если спросил сам — показываем")

    # источники ответа не дублируются в рекомендациях
    full = recommend(kb, cfg, Profile(msgs=1), "как открыть ИП")
    cut = recommend(kb, cfg, Profile(msgs=1), "как открыть ИП", exclude=set(full.items[:1]))
    assert full.items[0] not in cut.items
    print("OK источники ответа исключаются из рекомендаций")

    # тема не названа
    assert recommend(kb, cfg, Profile(), "посоветуй материалы", explicit=True).needs_topic
    assert not recommend(kb, cfg, Profile(fields={"sphere": "food"}), "что почитать", explicit=True).needs_topic
    print("OK «посоветуй материалы» без темы: спрашиваем тему, а с профилем берём сферу")

    # события: идентификатор чата хранится только хешем
    ev = Events(db_path.parent / "ev.db", "salt")
    ev.log(12345, "rec_shown", shown=["a"], paid=False)
    row = sqlite3.connect(db_path.parent / "ev.db").execute("SELECT user, type, payload FROM events").fetchone()
    assert row[0] != "12345" and len(row[0]) == 16 and row[1] == "rec_shown" and "12345" not in row[2]
    assert ev.user_hash(12345) == row[0] != Events(db_path.parent / "ev.db", "other").user_hash(12345)
    print("OK аналитика: чат хранится как хеш с солью")


async def flow(tmp: Path) -> None:
    from app.config import load_settings
    from app.dialog import LastQuestion, PendingStore
    from app.analytics import Events
    from app.kb.search import KnowledgeBase
    from app.llm import LLM
    from app.main import forget, on_question, toggle_recs
    from app.profile import ProfileStore

    cfg = load_settings()
    ctx = NS(bot_data=dict(cfg=cfg, llm=LLM(cfg), kb=KnowledgeBase(cfg.kb_path, cfg.min_coverage, cfg.min_dense),
                           pending=PendingStore(), last=LastQuestion(), profiles=ProfileStore(tmp / "bot.db"),
                           events=Events(tmp / "bot.db", "salt")))
    ev_db = sqlite3.connect(tmp / "bot.db")

    async def say(text):
        m = Msg(text)
        await on_question(NS(message=m, effective_chat=NS(id=7)), ctx)
        return m.sent[-1][0]

    # явная просьба: без вызова модели
    calls = len(mock_llm.CALLS)
    text = await say("посоветуй курс по маркетингу")
    assert "Вот что подобрал" in text and "🔒" in text and len(mock_llm.CALLS) == calls
    print("OK «посоветуй курс по маркетингу»: подборка без вызова модели")

    # без темы: спрашиваем, затем отвечаем подборкой
    text = await say("посоветуй материалы")
    assert "По какой теме" in text
    text = await say("налоги")
    assert "Вот что подобрал" in text, text
    print("OK без темы: бот спросил тему и подобрал материалы")

    # по ходу консультации: ответ + блок «Что ещё может пригодиться», источники не дублируются
    text = await say("Как открыть ИП?")
    assert "Источники" in text and "💡 Что ещё может пригодиться" in text, text
    sources = text.split("📚 Источники:")[1].split("💡")[0]
    for line in text.split("💡")[1].splitlines():
        if line.startswith("• Статья: "):
            assert line[len("• Статья: "):] not in sources, line
    print("OK после ответа добавлены рекомендации, источники не дублируются")

    # /recs отключает подсказки, но явная просьба работает
    m = Msg(); await toggle_recs(NS(message=m), ctx); assert "не буду добавлять" in m.sent[-1][0]
    text = await say("Как открыть ООО?")
    assert "Источники" in text and "💡" not in text
    assert "Вот что подобрал" in await say("что почитать про маркетинг")
    m = Msg(); await toggle_recs(NS(message=m), ctx); assert "включены" in m.sent[-1][0]
    print("OK /recs выключает подсказки после ответов, явная просьба всё равно работает")

    # аналитика
    types = {r[0] for r in ev_db.execute("SELECT DISTINCT type FROM events")}
    assert {"rec_request", "rec_shown", "answer"} <= types, types
    users = {r[0] for r in ev_db.execute("SELECT DISTINCT user FROM events")}
    assert len(users) == 1 and "7" not in users
    print("OK события записаны:", sorted(types))

    m = Msg(); await forget(NS(message=m), ctx)
    assert ctx.bot_data["profiles"].get(7).shown == []
    await ctx.bot_data["llm"].aclose()


if __name__ == "__main__":
    src = Path("data/kb.db")
    if not src.exists():
        sys.exit("Нет data/kb.db: сначала python -m app.kb.build")
    tmp = Path(tempfile.mkdtemp())
    shutil.copy(src, tmp / "kb.db")
    srv, base = mock_llm.start()
    os.environ.update(DATA_DIR=str(tmp), LLM_BASE_URL=base, LLM_MODEL="mock", EMBED_BASE_URL="", EMBED_MODEL="",
                      PLATFORM_BASE_URL="https://platform.test")
    unit(tmp / "kb.db")
    asyncio.run(flow(tmp))
    print("ВСЕ ПРОВЕРКИ РЕКОМЕНДАЦИЙ ПРОЙДЕНЫ")
