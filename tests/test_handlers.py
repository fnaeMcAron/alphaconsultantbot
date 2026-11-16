"""Проверка обработчиков Telegram на заглушках (без токена): python -m tests.test_handlers  (нужен data/kb.db)."""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from tests import mock_llm


class Chat:
    async def send_action(self, *_):
        pass


class Msg:
    def __init__(self, text=""):
        self.text, self.chat, self.chat_id, self.sent = text, Chat(), 7, []

    async def reply_text(self, text, **kw):
        self.sent.append((text, kw.get("reply_markup")))


class Query:
    def __init__(self, data, msg):
        self.data, self.message, self.edited = data, msg, False

    async def answer(self):
        pass

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edited = True


async def main() -> None:
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
    from app.main import on_option, on_question
    from app.profile import ProfileStore

    cfg = load_settings()
    ctx = NS(bot_data=dict(cfg=cfg, llm=LLM(cfg), kb=KnowledgeBase(cfg.kb_path, cfg.min_coverage, cfg.min_dense),
                           pending=PendingStore(), last=LastQuestion(),
                           profiles=ProfileStore(tmp / "bot.db")))

    def only_profile_buttons(markup) -> bool:
        """Допустимы только кнопки вопроса о бизнесе (prof:...), но не кнопки уточнения (opt:...)."""
        return markup is None or all(b.callback_data.startswith("prof:") for row in markup.inline_keyboard for b in row)

    async def say(text):
        m = Msg(text)
        await on_question(NS(message=m, effective_chat=NS(id=7)), ctx)
        return m.sent[-1]

    text, markup = await say("налоги")
    assert markup and len(markup.inline_keyboard) == 3 and ctx.bot_data["pending"].peek(7)
    print("OK «налоги» -> уточнение с 3 кнопками")

    m = Msg()
    q = Query("opt:0", m)
    await on_option(NS(callback_query=q), ctx)
    assert q.edited and m.sent and only_profile_buttons(m.sent[-1][1]), m.sent
    print("OK нажатие кнопки -> ответ, кнопки убраны:", m.sent[-1][0][:60].replace("\n", " "))

    await say("налоги")
    text, markup = await say("патент")
    assert only_profile_buttons(markup) and "Тема «" not in text
    print("OK короткий ответ на уточнение склеен с исходным вопросом")

    await say("налоги")
    text, markup = await say("Как открыть расчётный счёт для ИП?")
    assert only_profile_buttons(markup) and "Тема «" not in text
    print("OK новый вопрос вместо ответа на уточнение обработан отдельно")

    m = Msg()
    await on_option(NS(callback_query=Query("opt:0", m)), ctx)
    assert "устарел" in m.sent[-1][0]
    print("OK нажатие по устаревшей кнопке -> понятное сообщение")
    await ctx.bot_data["llm"].aclose()
    print("ВСЕ ПРОВЕРКИ ОБРАБОТЧИКОВ ПРОЙДЕНЫ")


if __name__ == "__main__":
    asyncio.run(main())
