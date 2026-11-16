"""Необязательный шаг: считает эмбеддинги фрагментов и кладёт их в kb.db.

    python -m app.kb.embed

Без эмбеддингов поиск работает на BM25. Можно прерывать и запускать повторно:
досчитываются только фрагменты без вектора.
"""
import logging
import sqlite3

import httpx
import numpy as np

from ..config import load_settings
from ..llm import auth_headers

log = logging.getLogger("kb.embed")
BATCH = 32


def run() -> int:
    cfg = load_settings()
    if not (cfg.embed_base_url and cfg.embed_model):
        raise SystemExit("Не заданы EMBED_BASE_URL и EMBED_MODEL.")
    db = sqlite3.connect(cfg.kb_path)
    rows = db.execute("""SELECT c.id, i.title, c.section, c.text FROM chunks c JOIN items i ON i.id=c.item_id
                         WHERE c.answerable=1 AND c.embedding IS NULL ORDER BY c.id""").fetchall()
    log.info("К расчёту: %d фрагментов", len(rows))
    done = 0
    with httpx.Client(timeout=cfg.llm_timeout) as http:
        for i in range(0, len(rows), BATCH):
            batch = rows[i:i + BATCH]
            inputs = [f"{t} {s}\n{x}"[:1500] for _, t, s, x in batch]
            r = http.post(f"{cfg.embed_base_url.rstrip('/')}/embeddings",
                          json={"model": cfg.embed_model, "input": inputs},
                          headers=auth_headers(cfg.embed_api_key))
            r.raise_for_status()
            vecs = [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
            for (cid, *_), v in zip(batch, vecs):
                db.execute("UPDATE chunks SET embedding=? WHERE id=?", (np.asarray(v, dtype=np.float32).tobytes(), cid))
            db.commit()
            done += len(batch)
            if (i // BATCH) % 20 == 0:
                log.info("%d / %d", done, len(rows))
    return done


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Готово:", run())
