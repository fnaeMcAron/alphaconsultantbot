"""Сквозная проверка пункта 1: python -m tests.test_pipeline  (нужен data/kb.db)."""
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
    os.environ.update(DATA_DIR=str(tmp), LLM_BASE_URL=base, LLM_MODEL="mock", EMBED_BASE_URL=base,
                      EMBED_MODEL="mock", PLATFORM_BASE_URL="https://platform.test")
    from app.answer import answer_question
    from app.config import load_settings
    from app.kb import embed
    from app.kb.search import KnowledgeBase
    from app.llm import LLM

    cfg = load_settings()
    assert embed.run() > 1000, "эмбеддинги не посчитались"
    kb = KnowledgeBase(cfg.kb_path, cfg.min_coverage, cfg.min_dense)
    assert kb.has_embeddings
    llm = LLM(cfg)

    r = await answer_question(cfg, kb, llm, "Как платить налоги на УСН?")
    assert r.status == "answered" and r.sources and "<think>" not in r.text, r
    assert r.sources[0].url.startswith("https://platform.test/") and "utm_source=telegram_bot" in r.sources[0].url
    print("OK answered:", r.sources[0].title)

    r = await answer_question(cfg, kb, llm, "Рецепт борща с говядиной")
    assert r.status == "no_answer", r
    print("OK no_answer (модель вернула НЕТ_ОТВЕТА)")

    r = await answer_question(cfg, kb, llm, "расскажи анекдот про курицу и яйцо без смысла")
    print("info: расплывчатый вопрос ->", r.status)

    os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9/v1"
    llm_down = LLM(load_settings())
    r = await answer_question(load_settings(), kb, llm_down, "Как платить налоги на УСН?")
    assert r.status == "llm_unavailable" and r.sources, r
    print("OK llm_unavailable: показан фрагмент из базы")
    await llm.aclose(); await llm_down.aclose()
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")


if __name__ == "__main__":
    asyncio.run(main())
