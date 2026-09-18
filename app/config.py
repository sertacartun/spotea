import os
import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CHART_COUNTRIES = "US,GB,CA,AU,IE,NZ"
# The value .env.example shipped with: public, so an install still using it has no secret at all.
PLACEHOLDER_SECRET_KEYS = {"change-me-too"}


class Settings(BaseSettings):
    # Unset means generate one on first start and keep it under ./data (see resolve_secret_key).
    secret_key: str | None = None
    database_url: str = "sqlite:////app/data/spotea.db"
    storage_dir: Path = Path("/app/data/storage")
    avatars_dir: Path = Path("/app/data/avatars")
    thumbnails_dir: Path = Path("/app/data/thumbnails")
    audio_format: str = "m4a"
    # Off by default: over plain HTTP on a LAN a Secure cookie makes login silently impossible.
    session_https_only: bool = False
    # Comma-separated country codes; charts are blended a rank at a time.
    music_chart_countries: str = DEFAULT_CHART_COUNTRIES
    # Deprecated; still read because extra="ignore" would otherwise drop an old .env value silently.
    music_chart_country: str | None = None
    # Once per 12-hour window the server asks GitHub whether a newer Spotea was released, and
    # tells the first-registered account. Off, nothing this app does ever leaves the machine.
    update_check: bool = True

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @property
    def chart_countries(self) -> list[str]:
        raw = self.music_chart_countries
        if self.music_chart_country and self.music_chart_countries == DEFAULT_CHART_COUNTRIES:
            # The deprecated setting only wins while the new one is still the default.
            raw = self.music_chart_country
        codes = [code.strip().upper() for code in raw.split(",") if code.strip()]
        return codes or DEFAULT_CHART_COUNTRIES.split(",")


def resolve_secret_key(settings: Settings) -> str:
    """SECRET_KEY if one was given, else a random key persisted beside the storage dir.

    Persisted rather than generated per start so a restart or rebuild doesn't log everyone out.
    """
    if settings.secret_key and settings.secret_key not in PLACEHOLDER_SECRET_KEYS:
        return settings.secret_key
    path = settings.storage_dir.parent / "secret_key"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = path.read_text().strip()
        if key:
            return key
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    key = secrets.token_hex(32)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return key


settings = Settings()
