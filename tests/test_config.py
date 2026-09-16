import pytest

from app.config import DEFAULT_CHART_COUNTRIES, PLACEHOLDER_SECRET_KEYS, Settings, resolve_secret_key

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


def _unset_key_settings(tmp_path, secret_key=None):
    # Explicit secret_key: conftest exports SECRET_KEY, which would otherwise win.
    return Settings(_env_file=None, storage_dir=tmp_path / "data" / "storage", secret_key=secret_key)


def test_a_given_secret_key_is_used_and_nothing_is_written(tmp_path):
    settings = _unset_key_settings(tmp_path, secret_key="from-env")

    assert resolve_secret_key(settings) == "from-env"
    assert not (tmp_path / "data" / "secret_key").exists()


def test_no_secret_key_generates_one_and_keeps_it_across_restarts(tmp_path):
    settings = _unset_key_settings(tmp_path)

    first = resolve_secret_key(settings)
    key_file = tmp_path / "data" / "secret_key"

    assert len(first) == 64
    assert key_file.read_text() == first
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert resolve_secret_key(_unset_key_settings(tmp_path)) == first


@pytest.mark.parametrize("placeholder", sorted(PLACEHOLDER_SECRET_KEYS))
def test_the_published_placeholder_counts_as_unset(tmp_path, placeholder):
    key = resolve_secret_key(_unset_key_settings(tmp_path, secret_key=placeholder))

    assert key != placeholder
    assert len(key) == 64


def test_an_empty_key_file_is_replaced(tmp_path):
    key_file = tmp_path / "data" / "secret_key"
    key_file.parent.mkdir(parents=True)
    key_file.write_text("")

    key = resolve_secret_key(_unset_key_settings(tmp_path))

    assert len(key) == 64
    assert key_file.read_text() == key
