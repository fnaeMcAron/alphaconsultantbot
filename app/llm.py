import logging
import re

import httpx

from .config import Settings

log = logging.getLogger("llm")
_THINK = re.compile(r"<think>.*?(</think>|$)", re.S | re.I)


class LLMError(Exception):
    pass


def auth_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"} if key else {}


class LLM:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self._http = httpx.AsyncClient(timeout=cfg.llm_timeout)

    async def chat(self, messages: list, *, temperature: float = 0.2, max_tokens: int = 900) -> str:
        cfg = self.cfg
        payload = {"model": cfg.llm_model, "messages": messages, "temperature": temperature,
                   "max_tokens": max_tokens, "stream": False}
        try:
            r = await self._http.post(cfg.llm_base_url.rstrip("/") + "/chat/completions",
                                      json=payload, headers=auth_headers(cfg.llm_api_key))
            r.raise_for_status()
            content = r.json()["choices"][0]["message"].get("content") or ""
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
            raise LLMError(f"{type(e).__name__}: {e}") from e
        return _THINK.sub("", content).strip()  # если llm может присылать рассуждения

    async def embed(self, texts: list) -> list:
        cfg = self.cfg
        if not (cfg.embed_base_url and cfg.embed_model):
            return []
        try:
            r = await self._http.post(cfg.embed_base_url.rstrip("/") + "/embeddings",
                                      json={"model": cfg.embed_model, "input": texts},
                                      headers=auth_headers(cfg.embed_api_key))
            r.raise_for_status()
            return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("Эмбеддинги недоступны, ищу только по словам: %s", e)
            return []

    async def aclose(self) -> None:
        await self._http.aclose()
