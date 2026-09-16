from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CHART_COUNTRIES = "US,GB,CA,AU,IE,NZ"


class Settings(BaseSettings):
    secret_key: str
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

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @property
    def chart_countries(self) -> list[str]:
        raw = self.music_chart_countries
        if self.music_chart_country and self.music_chart_countries == DEFAULT_CHART_COUNTRIES:
            # The deprecated setting only wins while the new one is still the default.
            raw = self.music_chart_country
        codes = [code.strip().upper() for code in raw.split(",") if code.strip()]
        return codes or DEFAULT_CHART_COUNTRIES.split(",")


settings = Settings()
