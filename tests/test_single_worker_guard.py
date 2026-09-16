"""app.main._assert_single_worker refuses to start under more than one uvicorn worker."""

import pytest

from app.main import _assert_single_worker


def test_no_web_concurrency_set_is_fine(monkeypatch):
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    _assert_single_worker()


def test_web_concurrency_of_one_is_fine(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    _assert_single_worker()


@pytest.mark.parametrize("value", ["2", "4", "0"])
def test_any_other_web_concurrency_refuses_to_start(monkeypatch, value):
    monkeypatch.setenv("WEB_CONCURRENCY", value)
    with pytest.raises(RuntimeError, match="WEB_CONCURRENCY"):
        _assert_single_worker()
