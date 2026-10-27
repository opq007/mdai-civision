"""Application settings, loaded from the environment."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- core -------------------------------------------------------------
    app_name: str = "mdai-civision"
    secret_key: str = "change-me-in-production"
    database_url: str = "sqlite+aiosqlite:///./data/civision.db"
    storage_dir: Path = Path("./data/videos")
    public_base_url: str = "http://127.0.0.1:8000"

    # --- admin ------------------------------------------------------------
    admin_username: str = "admin"
    admin_password: str = ""

    # --- limits -----------------------------------------------------------
    rate_limit_rpm: int = 60
    video_ttl_days: int = 7
    max_queue_size: int = 500
    worker_concurrency: int = 4

    # --- account behaviour ------------------------------------------------
    session_check_interval_s: int = 900
    # The daily magicube grant is credited by reading /api/v1/users/login/info,
    # and a second read the same day does nothing. Checking far more often than
    # once a day only makes the traffic look mechanical, so the loop wakes
    # hourly and skips any account touched within this window.
    daily_grant_min_interval_s: int = 12 * 60 * 60
    # Beijing-time window the grant may be claimed in. Upstream does not care
    # when the read happens, but a request at 03:00 looks nothing like someone
    # opening the site, so claims stay inside daytime hours.
    daily_grant_window_start_hour: int = 8
    daily_grant_window_end_hour: int = 21
    session_validate_before_use: bool = True
    submit_cooldown_s: int = 45
    account_error_cooldown_s: int = 60
    max_account_retries: int = 3
    default_max_concurrency: int = 1
    # Keep the egress IP stable. An account imported through a browser was
    # validated from that browser's IP; silently moving it to a pool proxy
    # is exactly the kind of identity change upstream risk control reacts to.
    auto_assign_proxy: bool = False
    # httpx honours HTTP_PROXY / HTTPS_PROXY / ALL_PROXY by default. A proxy
    # inherited from the container environment changes the egress IP without
    # appearing in the account row, so it is opt-in here. This maps to
    # httpx's `trust_env`, which also lets NETRC / SSL_CERT_FILE override the
    # client defaults, not just proxies.
    trust_env_proxy: bool = False
    # Large models at 720p can take a long time upstream (measured: a 14B
    # model at 720x1280 was still running after 25 minutes).
    poll_interval_s: float = 5.0
    poll_timeout_s: int = 60 * 60

    # --- browser (OAuth capture / renewal) --------------------------------
    browser_profile_dir: Path = Path("./.browser-profile")
    playwright_headless: bool = True

    # --- alerting ---------------------------------------------------------
    alert_webhook_url: str = ""

    # --- upstream ---------------------------------------------------------
    civision_base: str = "https://www.modelscope.ai"
    civision_timeout_s: float = 60.0
    upstream_rate_limit_s: int = 45

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def ensure_dirs(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        if self.is_sqlite:
            db_path = self.database_url.split("///")[-1]
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
