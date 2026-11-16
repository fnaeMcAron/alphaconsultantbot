"""Поиск по базе знаний.

BM25 (SQLite FTS5) по основам слов + необязательный векторный поиск (если посчитаны эмбеддинги,
см. `python -m app.kb.embed`). Кандидаты объединяются по Reciprocal Rank Fusion. Релевантность решают
пороги: доля слов запроса во фрагменте (`min_coverage`) или косинусная близость (`min_dense`).
"""
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from .text import query_tokens, tokens

RRF_K = 60


@dataclass
class Hit:
    chunk_id: int
    item_id: str
    kind: str
    title: str
    section: str
    text: str
    url: str
    coverage: float            # доля слов запроса (с весом по редкости), найденных во фрагменте, 0..1
    dense: Optional[float]     # косинусная близость, если есть эмбеддинги
    score: float = 0.0         # балл слияния рангов


@dataclass
class Candidate:
    item_id: str
    kind: str
    title: str
    description: str
    url: str
    access: str                # free | paid | subscription
    cost: Optional[int]
    industries: list
    meta: dict
    coverage: float            # по названию и лучшему фрагменту
    card_coverage: float = 0.0  # по названию и описанию (карточке материала)


@dataclass
class SearchResult:
    relevant: bool
    hits: list = field(default_factory=list)  # релевантные фрагменты; при relevant=False это ближайшие кандидаты


class KnowledgeBase:
    def __init__(self, path, min_coverage: float = 0.45, min_dense: float = 0.55,
                 max_chunks: int = 4, per_item: int = 2, pool: int = 40):
        self.path = Path(path)
        self.min_coverage, self.min_dense = min_coverage, min_dense
        self.max_chunks, self.per_item, self.pool = max_chunks, per_item, pool
        self._ids = self._mat = None
        self._row = {}
        self._df = {}
        db = self._connect()
        try:
            self._n = db.execute("SELECT COUNT(*) FROM chunks WHERE answerable=1").fetchone()[0]
        finally:
            db.close()
        self._load_embeddings()

    def _connect(self):
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)

    def _load_embeddings(self) -> None:
        db = self._connect()
        try:
            rows = db.execute("SELECT id, embedding FROM chunks WHERE answerable=1 AND embedding IS NOT NULL").fetchall()
        finally:
            db.close()
        if not rows:
            return
        mat = np.vstack([np.frombuffer(b, dtype=np.float32) for _, b in rows])
        mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
        self._ids, self._mat = [r[0] for r in rows], mat
        self._row = {cid: i for i, cid in enumerate(self._ids)}

    @property
    def has_embeddings(self) -> bool:
        return self._mat is not None

    def _weights(self, q: list) -> dict:
        """Вес слова запроса = IDF. Слово, которого нет в базе, самое «тяжёлое»: его отсутствие во фрагменте
        сильно снижает покрытие. Служебные слова, встречающиеся везде, весят почти ноль."""
        missing = [t for t in q if t not in self._df]
        if missing:
            db = self._connect()
            try:
                for t in missing:
                    self._df[t] = db.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                                             (f'"{t}"',)).fetchone()[0]
            finally:
                db.close()
        return {t: math.log((self._n + 1) / (self._df[t] + 1)) + 0.05 for t in q}

    def _lexical(self, q: list) -> list:
        match = " OR ".join(f'"{t}"' for t in q)
        db = self._connect()
        try:
            rows = db.execute(
                """SELECT c.id FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid
                   JOIN items i ON i.id = c.item_id
                   WHERE chunks_fts MATCH ? AND c.answerable = 1 AND i.published = 1
                   ORDER BY bm25(chunks_fts, 3.0, 1.0) LIMIT ?""", (match, self.pool)).fetchall()
        finally:
            db.close()
        return [r[0] for r in rows]

    def _dense(self, qvec) -> list:
        v = np.asarray(qvec, dtype=np.float32)
        v = v / (np.linalg.norm(v) + 1e-9)
        sims = self._mat @ v
        top = np.argsort(-sims)[: self.pool]
        return [self._ids[i] for i in top]

    def catalog_candidates(self, question: str, extra: str = "", pool: int = 150) -> list:
        """Материалы каталога для рекомендаций: опубликованные, не закрытые, любого доступа (в том числе платные).
        У платных материалов в индексе только карточка (название и описание), текст уроков туда не попадает."""
        q = list(dict.fromkeys(query_tokens(question)))
        if not q:
            return []
        more = [t for t in dict.fromkeys(query_tokens(extra)) if t not in q]
        match = " OR ".join(f'"{t}"' for t in q + more)
        db = self._connect()
        try:
            rows = db.execute(
                """SELECT i.id, i.kind, i.title, i.description, i.url, i.access, i.cost, i.industries, i.meta, c.text
                   FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid JOIN items i ON i.id = c.item_id
                   WHERE chunks_fts MATCH ? AND i.published = 1 AND i.is_closed = 0 AND i.kind != 'glossary'
                   ORDER BY bm25(chunks_fts, 3.0, 1.0) LIMIT ?""", (match, pool)).fetchall()
        finally:
            db.close()
        weight = self._weights(q)
        total, qset, seen = sum(weight.values()) or 1.0, set(q), {}
        for iid, kind, title, desc, url, access, cost, ind, meta, text in rows:
            if iid in seen:
                continue
            cov = sum(weight[t] for t in qset & set(tokens(f"{title} {text}"))) / total
            card = sum(weight[t] for t in qset & set(tokens(f"{title} {(desc or '')[:600]}"))) / total
            seen[iid] = Candidate(iid, kind, title.replace("\xa0", " "), desc, url, access, cost,
                                  json.loads(ind or "[]"), json.loads(meta or "{}"), cov, card)
        return list(seen.values())

    def vagueness(self, question: str, generic_df: float = 0.08) -> Optional[str]:
        """'empty' — в вопросе нет содержательных слов; 'generic' — одно слишком общее слово
        (например, «налоги»); None — вопрос достаточно конкретный."""
        q = list(dict.fromkeys(query_tokens(question)))
        if not q:
            return "empty"
        if len(q) == 1:
            self._weights(q)
            if self._n and self._df[q[0]] / self._n >= generic_df:
                return "generic"
        return None

    def suggest_titles(self, question: str, n: int = 3) -> list:
        """Названия материалов, в которых слова запроса есть в заголовке: варианты для уточнения."""
        q = list(dict.fromkeys(query_tokens(question)))
        if not q:
            return []
        match = " OR ".join(f'title : "{t}"' for t in q)
        db = self._connect()
        try:
            rows = db.execute(
                """SELECT i.id, i.title FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid
                   JOIN items i ON i.id = c.item_id
                   WHERE chunks_fts MATCH ? AND c.answerable = 1 AND i.published = 1
                     AND i.kind IN ('article', 'course', 'document', 'webinar', 'podcast')
                   ORDER BY bm25(chunks_fts, 3.0, 1.0) LIMIT 30""", (match,)).fetchall()
        finally:
            db.close()
        seen, out = set(), []
        for iid, title in rows:
            if iid not in seen and title:
                seen.add(iid)
                out.append(title.replace("\xa0", " "))
            if len(out) >= n:
                break
        return out

    def search(self, question: str, qvec=None, extra: str = "") -> SearchResult:
        """extra — дополнительные слова (например, из профиля): подтягивают материалы в выдаче,
        но не участвуют в оценке релевантности."""
        q = list(dict.fromkeys(query_tokens(question)))
        more = [t for t in dict.fromkeys(query_tokens(extra)) if t not in q]
        lex = self._lexical(q + more) if q else []
        den = self._dense(qvec) if (qvec is not None and self.has_embeddings) else []
        score = defaultdict(float)
        for ranking in (lex, den):
            for r, cid in enumerate(ranking):
                score[cid] += 1.0 / (RRF_K + r)
        ids = sorted(score, key=score.get, reverse=True)[: self.pool]
        if not ids:
            return SearchResult(False, [])

        db = self._connect()
        try:
            marks = ",".join("?" * len(ids))
            rows = {r[0]: r for r in db.execute(
                f"""SELECT c.id, c.item_id, i.kind, i.title, c.section, c.text, i.url
                    FROM chunks c JOIN items i ON i.id = c.item_id WHERE c.id IN ({marks})""", ids)}
        finally:
            db.close()

        weight = self._weights(q)
        total = sum(weight.values()) or 1.0
        qset = set(q)
        qv = None
        if qvec is not None and self.has_embeddings:
            qv = np.asarray(qvec, dtype=np.float32)
            qv = qv / (np.linalg.norm(qv) + 1e-9)
        selected, per = [], defaultdict(int)
        for cid in ids:
            if cid not in rows or per[rows[cid][1]] >= self.per_item:
                continue
            _, iid, kind, title, section, text, url = rows[cid]
            cov = sum(weight[t] for t in qset & set(tokens(f"{title} {section} {text}"))) / total
            dense = float(self._mat[self._row[cid]] @ qv) if qv is not None and cid in self._row else None
            per[iid] += 1
            selected.append(Hit(cid, iid, kind, title, section, text, url, cov, dense, score[cid]))
            if len(selected) >= self.max_chunks:
                break
        good = [h for h in selected if h.coverage >= self.min_coverage or (h.dense is not None and h.dense >= self.min_dense)]
        return SearchResult(bool(good), good if good else selected)
