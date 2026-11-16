"""Проверка профиля бизнеса: python -m tests.test_profile  (нужен data/kb.db; вместо Ollama заглушка)."""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from tests import mock_llm
from tests.test_handlers import Msg, Query


def unit() -> None:
    from app.profile import (Profile, ProfileStore, boost_terms, extract_profile, is_statement_only, next_question,
                             parse_field)
    # извлечение из утверждений от первого лица
    assert extract_profile("у меня ИП на патенте") == {"legal_form": "ip", "tax_regime": "patent"}
    assert extract_profile("Мы ООО на упрощёнке, доходы минус расходы") == {"legal_form": "ooo", "tax_regime": "usn_income_expense"}
    assert extract_profile("я самозанятый, делаю торты") == {"legal_form": "selfemployed", "tax_regime": "npd"}
    assert extract_profile("продаю на Ozon, открыл недавно")["sphere"] == "marketplaces"
    assert extract_profile("планирую открыть кофейню")["stage"] == "idea"
    # вопросы и чужие слова профиль не меняют
    for q in ("Как платить налоги на УСН?", "что лучше: патент или УСН", "налоги на УСН для ИП", "Как открыть ООО?"):
        assert extract_profile(q) == {}, q
    print("OK извлечение: утверждения да, вопросы нет")

    # ответы на вопрос бота
    assert parse_field("tax_regime", "УСН") == "usn" and parse_field("tax_regime", "пока не знаю") == "unknown"
    assert parse_field("tax_regime", "хочу борщ") is None and parse_field("sphere", "у меня кафе") == "food"
    assert is_statement_only("у меня ИП на патенте") and not is_statement_only("у меня ИП, как платить налоги")
    print("OK ответы на вопросы бота и различение утверждения/вопроса")

    # когда спрашивать
    p = Profile()
    assert next_question(p, "Как платить налоги на УСН?") == "tax_regime"
    p.asked.append("tax_regime"); p.msgs = 1; p.last_asked = 1
    assert next_question(p, "а патент?") is None, "не спрашиваем повторно"
    p.msgs = 4
    assert next_question(p, "Как открыть ИП?") == "stage", "после 3 ответов можно спросить этап"
    q = Profile(fields={"tax_regime": "patent"})
    assert next_question(q, "Как платить налоги?") is None, "известное не спрашиваем"
    assert next_question(Profile(fields={"tax_regime": "unknown"}), "налоги") is None, "«не знаю» уважаем"
    assert boost_terms({"tax_regime": "patent"}, "как платить налоги") == "патент"
    assert boost_terms({"tax_regime": "patent"}, "налоги на УСН") == ""
    print("OK правила вопросов: один раз, не чаще раза в 3 ответа, «не знаю» уважается")

    # хранилище
    with tempfile.TemporaryDirectory() as d:
        st = ProfileStore(Path(d) / "bot.db")
        pr = st.get(5); pr.set("tax_regime", "npd"); pr.asked.append("sphere"); st.save(5, pr)
        again = ProfileStore(Path(d) / "bot.db").get(5)
        assert again.fields == {"tax_regime": "npd", "legal_form": "selfemployed"} and again.asked == ["sphere"]
        assert "НПД" in again.summary() and "самозанятый" in again.summary()
        st.delete(5); assert st.get(5).fields == {}
    print("OK хранилище: сохранение, чтение после перезапуска, удаление")


async def flow() -> None:
    src = Path("data/kb.db")
    if not src.exists():
        sys.exit("Нет data/kb.db: сначала python -m app.kb.build")
    tmp = Path(tempfile.mkdtemp())
    shutil.copy(src, tmp / "kb.db")
    srv, base = mock_llm.start()
    os.environ.update(DATA_DIR=str(tmp), LLM_BASE_URL=base, LLM_MODEL="mock", EMBED_BASE_URL="", EMBED_MODEL="",
                      PLATFORM_BASE_URL="https://platform.test")
    from app.config import load_settings
    from app.dialog import LastQuestion, PendingStore
    from app.kb.search import KnowledgeBase
    from app.llm import LLM
    from app.main import forget, on_profile_button, on_question, show_profile
    from app.profile import ProfileStore

    cfg = load_settings()
    ctx = NS(bot_data=dict(cfg=cfg, llm=LLM(cfg), kb=KnowledgeBase(cfg.kb_path, cfg.min_coverage, cfg.min_dense),
                           pending=PendingStore(), last=LastQuestion(), profiles=ProfileStore(tmp / "bot.db")))
    store = ctx.bot_data["profiles"]

    async def say(text):
        m = Msg(text)
        await on_question(NS(message=m, effective_chat=NS(id=7)), ctx)
        return m.sent

    # вопрос про налоги не должен записать «УСН» в профиль, но после ответа бот спрашивает режим
    sent = await say("Как платить налоги на УСН?")
    text, markup = sent[-1]
    assert store.get(7).fields == {}, "из вопроса профиль не заполняется"
    assert markup and markup.inline_keyboard[0][0].callback_data == "prof:tax_regime:0" and "режим налогообложения" in text
    assert ctx.bot_data["pending"].peek(7).kind == "profile"
    print("OK после налогового вопроса: ответ + один вопрос о режиме, профиль не заполнен")

    # нажатие кнопки: запись и повторный ответ с учётом профиля (модель получает профиль в промпте)
    m = Msg(); q = Query("prof:tax_regime:2", m)
    mock_llm.LAST.clear()
    await on_profile_button(NS(callback_query=q), ctx)
    assert store.get(7).fields["tax_regime"] == "patent" and q.edited
    assert "Записал: патент" in m.sent[-1][0] and m.sent[-1][1] is None
    body = mock_llm.LAST["body"]
    assert "налоговый режим: патент" in body["messages"][-1]["content"] and "7." in body["messages"][0]["content"]
    print("OK кнопка -> профиль сохранён, повторный ответ получил профиль в промпте")

    # повторно про налоги не спрашиваем
    sent = await say("Когда платить налоги?")
    assert sent[-1][1] is None or all("tax_regime" not in b.callback_data for r in sent[-1][1].inline_keyboard for b in r)
    print("OK режим повторно не спрашивается")

    # утверждение: запомнили, на вопрос не отвечали
    n_llm = len(mock_llm.CALLS)
    sent = await say("у меня ИП, продаю на Ozon")
    assert "Запомнил" in sent[-1][0] and len(mock_llm.CALLS) == n_llm, "на утверждение модель не вызывается"
    assert store.get(7).known()["legal_form"] == "ip" and store.get(7).known()["sphere"] == "marketplaces"
    print("OK утверждение -> «Запомнил», без вызова модели")

    # /profile и /forget
    m = Msg(); await show_profile(NS(message=m), ctx); assert "патент" in m.sent[-1][0] and "ИП" in m.sent[-1][0]
    m = Msg(); await forget(NS(message=m), ctx); assert store.get(7).fields == {} and "удалены" in m.sent[-1][0]
    m = Msg(); await show_profile(NS(message=m), ctx); assert "ничего не знаю" in m.sent[-1][0]
    print("OK /profile показывает, /forget удаляет")

    # «не знаю» не раздражает
    await say("Как платить налоги на УСН?")
    sent = await say("пока не знаю")
    assert store.get(7).fields["tax_regime"] == "unknown" and "без этого уточнения" in sent[-1][0]
    print("OK «не знаю» принято, дальше не спрашиваем")
    await ctx.bot_data["llm"].aclose()


if __name__ == "__main__":
    unit()
    asyncio.run(flow())
    print("ВСЕ ПРОВЕРКИ ПРОФИЛЯ ПРОЙДЕНЫ")
