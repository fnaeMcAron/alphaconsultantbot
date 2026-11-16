"""Сборка локального индекса kb.db из дампа «Альфа-Курса».

    python -m app.kb.build [путь_к_дампу] [путь_к_kb.db]

Правила доступа (ограничение кейса №06): в индекс для ответов попадают только
опубликованные, не закрытые и бесплатные материалы. У платных курсов и курсов
по подписке есть только карточка для рекомендаций, текст уроков не читается.
"""
import json
import logging
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from .dump import load_tables, truthy
from .text import chunk_text, html_to_text, norm

log = logging.getLogger("kb.build")

TABLES = [
    "Articles", "ArticleEmbedItems", "Glossary", "GlossaryEmbedItems",
    "Courses", "CourseTypes", "Lessons", "LessonEmbedItems", "CourseLessonBindings",
    "Webinars", "Podcasts", "Documents", "Calculators", "Statuses",
    "ContentSettings", "ContentSettingsTopics", "ContentSettingsIndustries",
    "ContentSettingsTags", "Topics", "Industries", "Tags",
    "Subscription", "SubscriptionTariffs",
]

COURSE_ACCESS = {"Открытый": "free", "Платный": "paid", "По подписке": "subscription", "Закрытый": "closed"}

SCHEMA = """
DROP TABLE IF EXISTS items; DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS chunks_fts; DROP TABLE IF EXISTS plans;
CREATE TABLE items (
  id TEXT PRIMARY KEY, kind TEXT, src_id TEXT, title TEXT, description TEXT, url TEXT,
  access TEXT,            -- free | paid | subscription | closed
  is_closed INTEGER, published INTEGER, cost INTEGER, duration TEXT,
  topics TEXT, industries TEXT, tags TEXT, meta TEXT
);
CREATE TABLE chunks (
  id INTEGER PRIMARY KEY, item_id TEXT, section TEXT, ord INTEGER, text TEXT,
  answerable INTEGER,     -- 1: можно отвечать по тексту, 0: только для рекомендаций
  embedding BLOB
);
CREATE VIRTUAL TABLE chunks_fts USING fts5(title, body, tokenize='unicode61');
CREATE TABLE plans (subscription TEXT, tariff TEXT, cost INTEGER, discount_cost INTEGER,
                    recurrence TEXT, recurrence_value TEXT, is_main INTEGER, url TEXT);
"""


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _group(rows, key):
    d = defaultdict(list)
    for r in rows:
        d[r[key]].append(r)
    return d


def build(dump_path, out_path) -> dict:
    t = load_tables(dump_path, TABLES)
    published_id = next((s["Id"] for s in t["Statuses"] if s["Title"] == "Опубликовано"), None)
    ctype = {r["Id"]: r["Title"] for r in t["CourseTypes"]}

    # Темы, отрасли и теги по ContentSettings (полиморфная связь EntityId -> ContentSettings.Id)
    cs_of = {r["EntityId"]: r["Id"] for r in t["ContentSettings"]}
    names = {
        "topics": ({r["Id"]: r["Title"] for r in t["Topics"]}, _group(t["ContentSettingsTopics"], "ContentSettingsId"), "TopicId"),
        "industries": ({r["Id"]: r["Title"] for r in t["Industries"]}, _group(t["ContentSettingsIndustries"], "ContentSettingsId"), "IndustryId"),
        "tags": ({r["Id"]: r["Title"] for r in t["Tags"]}, _group(t["ContentSettingsTags"], "ContentSettingsId"), "TagId"),
    }

    def labels(entity_id, which):
        dic, links, col = names[which]
        cs = cs_of.get(entity_id)
        return sorted({dic[x[col]] for x in links.get(cs, []) if x[col] in dic})

    def is_pub(row, status=True):
        if truthy(row.get("IsDeleted")):
            return False
        return (row.get("StatusId") == published_id) if status else True

    items, texts = [], defaultdict(list)  # texts[item_id] = [(section, text), ...]

    def add_item(kind, row, *, access="free", status=True, cost=None, extra=None):
        iid = f"{kind}:{row['Id']}"
        desc = html_to_text(row.get("Description"))
        if row.get("DescriptionExt"):
            desc = (desc + "\n" + html_to_text(row["DescriptionExt"])).strip()
        closed = truthy(row.get("IsClosed"))
        published = is_pub(row, status)
        items.append(dict(
            id=iid, kind=kind, src_id=row["Id"], title=(row.get("Title") or "").strip(), description=desc,
            url=row.get("Url") or "", access=access, is_closed=int(closed), published=int(published),
            cost=_int(cost, None) if cost not in (None, "") else None, duration=row.get("Duration") or "",
            topics=labels(row["Id"], "topics"), industries=labels(row["Id"], "industries"),
            tags=labels(row["Id"], "tags"), meta=extra or {},
        ))
        return iid, published and not closed and access == "free"

    def embed_text(rows, fk, key):
        blocks = sorted((r for r in rows.get(key, []) if str(r["Type"]) == "0"), key=lambda r: _int(r["Order"]))
        return "\n".join(x for x in (html_to_text(b["HtmlContent"]) for b in blocks) if x)

    art_emb, gl_emb = _group(t["ArticleEmbedItems"], "ArticleId"), _group(t["GlossaryEmbedItems"], "GlossaryId")
    les_emb = _group(t["LessonEmbedItems"], "LessonId")

    answerable_item = {}
    for a in t["Articles"]:
        iid, ok = add_item("article", a, extra={"is_public": truthy(a.get("IsPublic"))})
        body = embed_text(art_emb, "ArticleId", a["Id"]) or html_to_text(a.get("Content"))
        texts[iid].append(("", body))
        answerable_item[iid] = ok
    for g in t["Glossary"]:
        iid, ok = add_item("glossary", g)
        texts[iid].append(("", embed_text(gl_emb, "GlossaryId", g["Id"])))
        answerable_item[iid] = ok

    lessons = {r["Id"]: r for r in t["Lessons"] if not truthy(r.get("IsDeleted")) and not truthy(r.get("IsGroup"))}
    bind = _group([b for b in t["CourseLessonBindings"] if not truthy(b.get("IsDeleted"))], "CourseId")
    for c in t["Courses"]:
        access = COURSE_ACCESS.get(ctype.get(c["TypeId"], ""), "closed")
        iid, ok = add_item("course", c, access=access, cost=c.get("Cost"), extra={
            "type": ctype.get(c["TypeId"]), "lessons": _int(c.get("LessonQuantity")),
            "certified": truthy(c.get("IsCertified")), "trial": truthy(c.get("HasTrial")),
            "target": html_to_text(c.get("Target")), "rating": c.get("Rating"),
        })
        answerable_item[iid] = ok
        if ok:  # тексты уроков читаем только у открытых курсов
            for b in sorted(bind.get(c["Id"], []), key=lambda x: _int(x["Order"])):
                les = lessons.get(b["LessonId"])
                if les is None or truthy(b.get("IsClosed")):
                    continue
                body = embed_text(les_emb, "LessonId", les["Id"]) or html_to_text(les.get("Content"))
                texts[iid].append((les["Title"], body))
    for w in t["Webinars"]:
        iid, ok = add_item("webinar", w, status=False, extra={"starts_at": str(w.get("StartsAt") or "")})
        answerable_item[iid] = ok
    for p in t["Podcasts"]:
        paid = p.get("Cost") not in (None, "")
        iid, ok = add_item("podcast", p, access="paid" if paid else "free", cost=p.get("Cost"))
        answerable_item[iid] = ok
    for d in t["Documents"]:
        iid, ok = add_item("document", d, extra={"extension": d.get("Extension")})
        answerable_item[iid] = ok
    for c in t["Calculators"]:
        iid, ok = add_item("calculator", c, status=False)
        answerable_item[iid] = ok

    # ---- запись в SQLite ----
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(out_path)
    db.executescript(SCHEMA)
    by_id = {i["id"]: i for i in items}
    n_chunks = 0
    for it in items:
        db.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            it["id"], it["kind"], it["src_id"], it["title"], it["description"], it["url"], it["access"],
            it["is_closed"], it["published"], it["cost"], it["duration"],
            json.dumps(it["topics"], ensure_ascii=False), json.dumps(it["industries"], ensure_ascii=False),
            json.dumps(it["tags"], ensure_ascii=False), json.dumps(it["meta"], ensure_ascii=False)))
        if not it["published"]:
            continue
        # карточка: название + описание, видна и платным материалам (для рекомендаций)
        pieces = [("", it["description"])] if it["description"] else []
        for section, body in texts.get(it["id"], []):
            pieces.extend((section, c) for c in chunk_text(body))
        for order, (section, text) in enumerate(pieces):
            if not text:
                continue
            is_card = order == 0 and section == "" and text == it["description"]
            answerable = int(answerable_item[it["id"]])
            cur = db.execute("INSERT INTO chunks (item_id, section, ord, text, answerable) VALUES (?,?,?,?,?)",
                             (it["id"], section, order, text, answerable))
            db.execute("INSERT INTO chunks_fts (rowid, title, body) VALUES (?,?,?)",
                       (cur.lastrowid, norm(f"{it['title']} {section}"), norm(text)))
            n_chunks += 1
    sub_title = {s["Id"]: s["Title"] for s in t["Subscription"]}
    for tr in t["SubscriptionTariffs"]:
        db.execute("INSERT INTO plans VALUES (?,?,?,?,?,?,?,?)", (
            sub_title.get(tr["SubscriptionId"], ""), tr["Title"], _int(tr["Cost"], None),
            _int(tr.get("DiscountCost"), None), tr.get("Recurrence"), tr.get("RecurrenceValue"),
            int(truthy(tr.get("IsMain"))), tr.get("Url")))
    db.commit()
    stats = {r[0]: r[1:] for r in db.execute(
        "SELECT kind, COUNT(*), SUM(published), SUM(is_closed) FROM items GROUP BY kind")}
    stats["_chunks"] = n_chunks
    stats["_answerable_chunks"] = db.execute("SELECT COUNT(*) FROM chunks WHERE answerable=1").fetchone()[0]
    db.close()
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    dump = sys.argv[1] if len(sys.argv) > 1 else "data/alfa_course_hackathon.backup"
    out = sys.argv[2] if len(sys.argv) > 2 else "data/kb.db"
    for k, v in build(dump, out).items():
        print(f"{k:22s}{v}")
