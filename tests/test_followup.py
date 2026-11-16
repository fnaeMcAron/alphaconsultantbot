"""Проверка реплик-продолжений и нумерации источников: python -m tests.test_followup  (нужен data/kb.db)."""
import asyncio
import os
import re
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
    from app.answer import answer_question, standalone_question
    from app.config import load_settings
    from app.dialog import LastQuestion
    from app.kb.search import KnowledgeBase
    from app.llm import LLM

    cfg = load_settings()
    kb = KnowledgeBase(cfg.kb_path, cfg.min_coverage, cfg.min_dense)
    llm = LLM(cfg)

    # 1. короткая реплика после вопроса превращается в самостоятельный вопрос
    prev = "Как открыть ИП?"
    mock_llm.CALLS.clear()
    q = await standalone_question(cfg, llm, "даже юридическому лицу?", prev)
    assert q == "Может ли юридическое лицо открыть ИП?" and mock_llm.CALLS == ["rewrite"], (q, mock_llm.CALLS)
    print("OK продолжение переписано:", q)

    # 2. без предыдущего вопроса или с длинной репликой модель для переписывания не вызывается
    mock_llm.CALLS.clear()
    assert await standalone_question(cfg, llm, "даже юридическому лицу?", "") == "даже юридическому лицу?"
    long_q = "Как мне открыть расчётный счёт для ИП без визита в банк"
    assert await standalone_question(cfg, llm, long_q, prev) == long_q
    assert mock_llm.CALLS == [], mock_llm.CALLS
    print("OK без контекста и для длинных вопросов лишнего вызова модели нет")

    # 3. мусорный ответ переписывателя (слишком длинный) игнорируется
    assert await standalone_question(cfg, llm, "ГАРБАЖ", prev) == "ГАРБАЖ"
    print("OK мусорная переписка отброшена")

    # 4. сквозной сценарий и сохранение формулировки для следующей реплики
    r = await answer_question(cfg, kb, llm, "даже юридическому лицу?", prev=prev)
    assert r.status in ("answered", "no_answer"), r
    print("OK сквозной:", r.status, "| standalone =", repr(r.standalone))

    # 5. нумерация источников подряд, ссылки в тексте переписаны
    r = await answer_question(cfg, kb, llm, "Как платить налоги на УСН?")
    nums = [s.n for s in r.sources]
    assert nums == list(range(1, len(nums) + 1)), nums
    assert all(int(n) <= len(nums) for n in re.findall(r"\[(\d+)\]", r.text.split("📚")[0])), r.text
    print("OK нумерация источников подряд:", nums)

    # 6. хранилище последнего вопроса
    ls = LastQuestion(ttl=600)
    ls.put(1, "Как открыть ИП?", now=100)
    assert ls.get(1, now=200) == "Как открыть ИП?" and ls.get(1, now=800) == "" and ls.get(2) == ""
    print("OK память последнего вопроса с истечением срока")
    await llm.aclose()
    print("ВСЕ ПРОВЕРКИ ПРОДОЛЖЕНИЙ ПРОЙДЕНЫ")


if __name__ == "__main__":
    asyncio.run(main())
