import pytest

from app.config import DEFAULT_CHART_COUNTRIES, Settings

DEFAULT = DEFAULT_CHART_COUNTRIES.split(",")


def _settings(**overrides):
    # _env_file=None so the repo's own .env can't leak in.
    return Settings(secret_key="test", _env_file=None, **overrides)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("ZZ", ["ZZ"]),
        ("TR", ["TR"]),
        ("TR,US,GB,DE", ["TR", "US", "GB", "DE"]),
        (" tr , us ", ["TR", "US"]),
        # Never empty: an empty setting means "the default", not "no charts".
        ("", DEFAULT),
        (",,", DEFAULT),
    ],
)
def test_chart_countries_parses_a_list(configured, expected):
    assert _settings(music_chart_countries=configured).chart_countries == expected


def test_the_old_single_country_setting_still_works():
    """MUSIC_CHART_COUNTRY would otherwise be silently dropped by extra="ignore" on upgrade."""
    assert _settings(music_chart_country="TR").chart_countries == ["TR"]


def test_the_new_setting_wins_when_both_are_present():
    settings = _settings(music_chart_country="TR", music_chart_countries="US,GB")

    assert settings.chart_countries == ["US", "GB"]


def test_the_default_is_the_english_speaking_markets():
    assert _settings().chart_countries == ["US", "GB", "CA", "AU", "IE", "NZ"]
    assert _settings(music_chart_countries="ZZ").chart_countries == ["ZZ"]


def test_the_deprecated_setting_is_not_pinned_to_a_country_code():
    assert _settings(music_chart_country="TR").chart_countries == ["TR"]
    assert "ZZ" not in DEFAULT_CHART_COUNTRIES
