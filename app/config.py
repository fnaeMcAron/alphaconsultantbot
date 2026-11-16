import os
from dataclasses import dataclass, field
from pathlib import Path

_KINDS = ["article", "course", "webinar", "podcast", "document", "calculator", "glossary"]


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    data_dir: Path
    dump_file: str
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    embed_base_url: str
    embed_model: str
    embed_api_key: str
    platform_base_url: str
    min_coverage: float = 0.6
    min_dense: float = 0.55
    vague_df: float = 0.08
    analytics_salt: str = "alphaconsultantbot"
    llm_timeout: float = 180.0
    telegram_proxy: str = ""
    link_templates: dict = field(default_factory=dict)

    @property
    def dump_path(self) -> Path:
        return self.data_dir / self.dump_file

    @property
    def kb_path(self) -> Path:
        return self.data_dir / "kb.db"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "bot.db"


def load_settings() -> Settings:
    env = os.environ.get
    return Settings(
        telegram_token=env("TELEGRAM_BOT_TOKEN", ""),
        data_dir=Path(env("DATA_DIR", "./data")),
        dump_file=env("DUMP_FILE", "alfa_course_hackathon.backup"),
        llm_base_url=env("LLM_BASE_URL", "http://host.docker.internal:11434/v1"),
        llm_model=env("LLM_MODEL", "qwen3:8b"),
        llm_api_key=env("LLM_API_KEY", "ollama"),
        embed_base_url=env("EMBED_BASE_URL", ""),
        embed_model=env("EMBED_MODEL", ""),
        embed_api_key=env("EMBED_API_KEY", ""),
        platform_base_url=env("PLATFORM_BASE_URL", "").rstrip("/"),
        min_coverage=float(env("MIN_COVERAGE", "0.6")),
        min_dense=float(env("MIN_DENSE", "0.55")),
        vague_df=float(env("VAGUE_DF", "0.08")),
        analytics_salt=env("ANALYTICS_SALT", "alphaconsultantbot"),
        llm_timeout=float(env("LLM_TIMEOUT", "180")),
        telegram_proxy=env("TELEGRAM_PROXY", ""),
        link_templates={
            k: env(f"LINK_TPL_{k.upper()}", "{base}/" + k + "s/{url}") for k in _KINDS
        },
    )
