from typing import Optional
from urllib.parse import urlencode

from .config import Settings


def links_configured(cfg: Settings) -> bool:
    return bool(cfg.platform_base_url) and not cfg.platform_base_url.endswith(".invalid")


def material_url(cfg: Settings, kind: str, url: str, campaign: str = "bot") -> Optional[str]:
    """ссылка с utm метками или none если домен платформы ещё не настроен"""
    if not url or not links_configured(cfg):
        return None
    tpl = cfg.link_templates.get(kind, "{base}/{url}")
    link = tpl.format(base=cfg.platform_base_url, url=url)
    utm = urlencode({"utm_source": "telegram_bot", "utm_medium": "assistant", "utm_campaign": campaign})
    return f"{link}{'&' if '?' in link else '?'}{utm}"
