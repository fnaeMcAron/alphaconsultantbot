"""Чтение дампа PostgreSQL (custom format) без установленного PostgreSQL."""
import re

import pgdumplib


def load_tables(path, names):
    """Возвращает {имя_таблицы: [dict, ...]} для схемы public."""
    dump = pgdumplib.load(str(path))
    cols = {}
    for e in dump.entries:
        if e.desc == "TABLE" and e.namespace == "public" and e.tag in names:
            cols[e.tag] = re.findall(r'^\s+"([^"]+)"\s', e.defn, re.M)
    out = {}
    for name in names:
        if name not in cols:
            out[name] = []
            continue
        out[name] = [dict(zip(cols[name], row)) for row in dump.table_data("public", name)]
    return out


def truthy(v) -> bool:
    return v in (True, "t", "true", "True", "1", 1)
