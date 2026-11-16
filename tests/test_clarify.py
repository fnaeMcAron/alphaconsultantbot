"""Проверка пункта 2 (уточнения): python -m tests.test_clarify  (нужен data/kb.db, без эмбеддингов)."""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

from tests import mock_llm


async def main() -> None:
    src = Path("data/kb.db")
    if not src.exists():
        sys.exit("Нет data/kb.db: сначала python -m app.kb.build")
    tmp = Path(tempfile.mkdtemp())
    shutil.copy(src, tmp / "kb.db")
    srv, base = mock_llm.start()
    os.environ.update(DATA_DIR=str(tmp), LLM_BASE_URL=base, LLM_MODEL="mock", EMBED_BASE_URL="", EMBED_MODEL="",
                      PLATFORM_BASE_URL="https://platform.test")
    from app.answer import answer_question
    from app.config import load_settings
    from app.dialog import PendingStore, looks_like_reply
    from app.kb.search import KnowledgeBase
    from app.llm import LLM

    cfg = load_settings()
    kb = KnowledgeBase(cfg.kb_path, cfg.min_coverage, cfg.min_dense)
    llm = LLM(cfg)

    # 1. слишком общий однословный вопрос: уточнение с вариантами из реальных материалов, без вызова модели
    for q in ("налоги", "бизнес", "клиенты"):
        r = await answer_question(cfg, kb, llm, q)
        assert r.status == "clarify" and r.expects_reply, (q, r)
        print(f"OK общий «{q}» -> clarify, вариантов: {len(r.options)}", r.options[:2])
    r = await answer_question(cfg, kb, llm, "налоги")
    assert len(r.options) >= 2, r

    # 2. нет содержательных слов
    r = await answer_question(cfg, kb, llm, "помоги пожалуйста")
    assert r.status == "clarify" and not r.expects_reply and not r.options, r
    print("OK пустой вопрос -> просьба переформулировать")

    # 3. конкретный однословный вопрос отвечаем сразу
    for q in ("УСН", "Ozon"):
        r = await answer_question(cfg, kb, llm, q)
        assert r.status != "clarify", (q, r)
    print("OK конкретные однословные («УСН», «Ozon») не уточняем")

    # 4. модель просит уточнение
    r = await answer_question(cfg, kb, llm, "подскажите как платить налоги на УСН")
    assert r.status == "clarify" and r.expects_reply and r.text.endswith("?"), r
    print("OK модель уточняет:", r.text)

    # 5. после уточнения второй раз не спрашиваем: отвечаем
    r = await answer_question(cfg, kb, llm, "ООО", context="подскажите как платить налоги на УСН")
    assert r.status in ("answered", "no_answer"), r
    print("OK после уточнения ->", r.status)

    # 6. длинный подробный вопрос не уточняем, даже если модель просит
    long_q = "подскажите как платить налоги на УСН если у меня ООО и два сотрудника и работаем только по безналу"
    r = await answer_question(cfg, kb, llm, long_q)
    assert r.status in ("answered", "no_answer"), r
    print("OK длинный вопрос не уточняется ->", r.status)

    # 7. состояние диалога
    ps = PendingStore(ttl=600)
    ps.put(1, "налоги", ["a", "b"], now=1000)
    assert ps.peek(1, now=1100).question == "налоги"
    assert ps.pop(1, now=1700) is None and ps.pop(1, now=1100) is None  # истёк срок / уже забрали
    assert looks_like_reply("УСН") and looks_like_reply("у меня ИП на патенте")
    assert not looks_like_reply("А как платить налоги в 2026 году?") and not looks_like_reply("как мне открыть расчётный счёт для ООО без визита в банк")
    print("OK память диалога и различение «ответ на уточнение / новый вопрос»")
    await llm.aclose()
    print("ВСЕ ПРОВЕРКИ УТОЧНЕНИЙ ПРОЙДЕНЫ")


if __name__ == "__main__":
    asyncio.run(main())
