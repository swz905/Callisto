"""Settings, read from the environment (and `.env` if python-dotenv is installed)."""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # optional
    pass


@dataclass(frozen=True)
class Settings:
    falkordb_host: str
    falkordb_port: int
    falkordb_password: str | None
    falkordb_username: str | None
    falkordb_ssl: bool
    user_id: str
    tz: str
    llm: str  # auto (OpenRouter if configured, rules offline) | openrouter | rules
    openrouter_api_key: str | None
    openrouter_model: str | None
    openrouter_extract_model: str | None
    openrouter_fallback_models: tuple[str, ...]
    openrouter_base_url: str
    llm_timeout: float
    embeddings: bool
    hub_url: str
    device_token: str
    sandbox_dir: str  # the only place delete_path may touch
    simulate_devices: bool  # hub plays offline devices (fake downloads etc.) for demos
    triage: str  # auto (LLM if available, else rules) | rules | off
    triage_max_per_min: int  # cap on LLM triage calls
    triage_notify_min: int  # importance (0-3) at which an unrequested alert is sent
    # --- integrations (all optional; empty = off) ---
    user_display_name: str = ""
    hub_public_url: str = "http://localhost:8000"  # how devices reach the hub over HTTP (file handoff links)
    data_dir: str = ""  # transfers etc.
    telegram_bot_token: str | None = None
    telegram_allowed_chat_ids: tuple[int, ...] = ()
    telegram_device_id: str = ""
    calendar_urls: tuple[str, ...] = ()  # ICS URLs / file paths / "demo"
    calendar_interval_s: int = 600
    imap_host: str | None = None
    imap_port: int = 993
    imap_user: str | None = None
    imap_password: str | None = None
    imap_folder: str = "INBOX"
    imap_interval_s: int = 120
    plugins_file: str = ""
    family_graph: str = ""
    # read-only web access (kairos/web.py)
    web_mode: str = "auto"  # auto | playwright | http | off
    web_engine: str = "duckduckgo"  # duckduckgo | bing
    web_headless: bool = True
    web_channel: str = "chrome"  # installed browser to drive: chrome | msedge | "" (Playwright's Chromium)
    web_timeout_s: float = 20.0
    web_max_chars: int = 6000
    # chat log + nightly memory digest (kairos/journal.py, kairos/digest.py)
    chatlog: bool = True  # one text file per day of conversation under <data_dir>/chatlog
    chatlog_days: int = 30  # digested logs older than this are deleted
    digest: str = "auto"  # auto (small LLM if available, else rules) | rules | off
    digest_model: str | None = None  # a small/cheap OpenRouter model; defaults to the extract model
    digest_hour: int = 3  # local hour after which the previous days are digested
    # speech-to-text on the hub (kairos/voice/stt.py) for the dashboard mic and the desktop pet
    stt_model: str = "base.en"  # faster-whisper size (tiny.en, base.en, small.en, ...) or a local model folder
    stt_device: str = "cpu"  # cpu | cuda
    stt_language: str = "en"  # empty = auto-detect (use a multilingual model, e.g. small)

    @property
    def graph_name(self) -> str:
        return f"kairos:{self.user_id}"

    def falkordb_kwargs(self) -> dict:
        """Connection args for FalkorDB(): local Docker, or a hosted instance (username + TLS)."""
        kw: dict = {"host": self.falkordb_host, "port": self.falkordb_port, "password": self.falkordb_password,
                    "socket_connect_timeout": 5}
        if self.falkordb_username:
            kw["username"] = self.falkordb_username
        if self.falkordb_ssl:
            kw["ssl"] = True
        return kw


def load_settings() -> Settings:
    env = os.environ.get
    return Settings(
        falkordb_host=env("FALKORDB_HOST", "localhost"),
        falkordb_port=int(env("FALKORDB_PORT", "6379")),
        falkordb_password=env("FALKORDB_PASSWORD") or None,
        falkordb_username=env("FALKORDB_USERNAME") or None,
        falkordb_ssl=env("FALKORDB_SSL", "0").lower() in ("1", "true", "yes"),
        user_id=env("KAIROS_USER", "arjun"),
        tz=env("KAIROS_TZ", "Asia/Kolkata"),
        llm=env("KAIROS_LLM", "auto").lower(),
        openrouter_api_key=env("OPENROUTER_API_KEY") or None,
        openrouter_model=env("OPENROUTER_MODEL") or None,
        openrouter_extract_model=env("OPENROUTER_EXTRACT_MODEL") or env("OPENROUTER_MODEL") or None,
        openrouter_fallback_models=tuple(m.strip() for m in env("OPENROUTER_FALLBACK_MODELS", "").split(",")
                                         if m.strip()),
        openrouter_base_url=env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
        llm_timeout=float(env("KAIROS_LLM_TIMEOUT", "20")),
        embeddings=env("KAIROS_EMBEDDINGS", "0") == "1",
        hub_url=env("KAIROS_HUB_URL", "ws://localhost:8000"),
        device_token=env("KAIROS_DEVICE_TOKEN", "dev-token"),
        sandbox_dir=env("KAIROS_SANDBOX") or os.path.join(os.path.expanduser("~"), "kairos_sandbox"),
        simulate_devices=env("KAIROS_SIMULATE_DEVICES", "1") == "1",
        triage=env("KAIROS_TRIAGE", "auto").lower(),
        triage_max_per_min=int(env("KAIROS_TRIAGE_MAX_PER_MIN", "20")),
        triage_notify_min=int(env("KAIROS_TRIAGE_NOTIFY_MIN", "3")),
        user_display_name=env("KAIROS_USER_NAME", ""),
        hub_public_url=env("KAIROS_HUB_PUBLIC_URL", "http://localhost:8000").rstrip("/"),
        data_dir=env("KAIROS_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".kairos"),
        telegram_bot_token=env("TELEGRAM_BOT_TOKEN") or None,
        telegram_allowed_chat_ids=tuple(int(x) for x in _csv(env("TELEGRAM_ALLOWED_CHAT_IDS", "")) if x.lstrip("-").isdigit()),
        telegram_device_id=env("TELEGRAM_DEVICE_ID", ""),
        calendar_urls=tuple(_csv(env("KAIROS_CALENDAR_URLS", ""))),
        calendar_interval_s=int(env("KAIROS_CALENDAR_INTERVAL", "600")),
        imap_host=env("KAIROS_IMAP_HOST") or None,
        imap_port=int(env("KAIROS_IMAP_PORT", "993")),
        imap_user=env("KAIROS_IMAP_USER") or None,
        imap_password=env("KAIROS_IMAP_PASSWORD") or None,
        imap_folder=env("KAIROS_IMAP_FOLDER", "INBOX"),
        imap_interval_s=int(env("KAIROS_IMAP_INTERVAL", "120")),
        plugins_file=env("KAIROS_PLUGINS_FILE", ""),
        family_graph=env("KAIROS_FAMILY_GRAPH", ""),
        web_mode=env("KAIROS_WEB", "auto").lower(),
        web_engine=env("KAIROS_WEB_ENGINE", "duckduckgo").lower(),
        web_headless=env("KAIROS_WEB_HEADLESS", "1") != "0",
        web_channel=env("KAIROS_WEB_CHANNEL", "chrome"),
        web_timeout_s=float(env("KAIROS_WEB_TIMEOUT", "20")),
        web_max_chars=int(env("KAIROS_WEB_MAX_CHARS", "6000")),
        chatlog=env("KAIROS_CHATLOG", "1") != "0",
        chatlog_days=int(env("KAIROS_CHATLOG_DAYS", "30")),
        digest=env("KAIROS_DIGEST", "auto").lower(),
        digest_model=env("KAIROS_DIGEST_MODEL") or env("OPENROUTER_EXTRACT_MODEL") or env("OPENROUTER_MODEL") or None,
        digest_hour=int(env("KAIROS_DIGEST_HOUR", "3")),
        stt_model=env("KAIROS_STT_MODEL", "base.en"),
        stt_device=env("KAIROS_STT_DEVICE", "cpu"),
        stt_language=env("KAIROS_STT_LANGUAGE", "en"),
    )


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]
