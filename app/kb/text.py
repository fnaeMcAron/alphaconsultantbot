"""Очистка HTML, нарезка на фрагменты и нормализация для полнотекстового поиска."""
import html
import re

import snowballstemmer

_stemmer = snowballstemmer.stemmer("russian")
_DROP = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_BLOCK = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|table|blockquote)[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[a-zа-я0-9]+")
_SENT = re.compile(r"(?<=[.!?])\s+")


def html_to_text(raw) -> str:
    if not raw:
        return ""
    s = _DROP.sub(" ", str(raw))
    s = _BLOCK.sub("\n", s)
    s = html.unescape(_TAG.sub("", s)).replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    lines = (re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n"))
    return "\n".join(ln for ln in lines if ln)


def chunk_text(text: str, max_chars: int = 900) -> list:
    """Жадно склеивает абзацы до max_chars; длинные абзацы режет по предложениям."""
    parts = []
    for para in text.split("\n"):
        if len(para) <= max_chars:
            parts.append(para)
        else:
            parts.extend(_SENT.split(para))
    chunks, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) + 1 > max_chars:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return [c for c in chunks if len(c) >= 40]


def tokens(text: str) -> list:
    words = _WORD.findall(text.lower().replace("ё", "е"))
    return [_stemmer.stemWord(w) for w in words]


def norm(text: str) -> str:
    """Строка из основ слов: её кладём в FTS5 и ею же ищем."""
    return " ".join(tokens(text))


_STOP = set("""как что это для и в во на с со по из к ко у о об а но или ли мне мой моя мое мои я мы вы он она они
нужно нужна можно какой какая какие какое чтобы при от до за не ни же бы есть быть был была было где когда почему
зачем кто чем там тут если то так также еще уже очень надо хочу хотел хотела подскажите скажите пожалуйста
расскажите про свой свою свои его ее их ему мной""".split())


def query_terms(query: str) -> list:
    """Основы значимых слов запроса (без стоп-слов), порядок сохраняется."""
    words = _WORD.findall(query.lower().replace("ё", "е"))
    return list(dict.fromkeys(_stemmer.stemWord(w) for w in words if w not in _STOP and len(w) > 1))


_STOP = set("""как что это для и в во на по с со к о об от до из за у не ли а но или же бы мне мой моя мое мои я мы вы он она они
нужно надо можно какой какая какие какое где когда почему зачем сколько чем есть быть при про над под без так тоже очень
пожалуйста подскажите расскажите скажите помогите помоги расскажи объясни объясните хочу хотим можете можешь""".split())


def query_tokens(text: str) -> list:
    """Основы слов запроса без служебных слов."""
    words = _WORD.findall(text.lower().replace("ё", "е"))
    return [_stemmer.stemWord(w) for w in words if w not in _STOP and len(w) > 1]
