import os
import sys
import types
from pathlib import Path

import pytest


class _DummyBot:
    def message_handler(self, *args, **kwargs):  # noqa: D401 - decorator stub
        def decorator(func):
            return func

        return decorator

    def callback_query_handler(self, *args, **kwargs):  # noqa: D401 - decorator stub
        def decorator(func):
            return func

        return decorator

    def __getattr__(self, name):
        def _stub(*args, **kwargs):
            return None

        return _stub


def _install_telebot_stub():
    telebot_mod = types.ModuleType("telebot")
    telebot_mod.TeleBot = lambda *args, **kwargs: _DummyBot()

    types_mod = types.ModuleType("telebot.types")
    class _DummyMarkup:  # noqa: D401 - simple placeholder
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _DummyButton:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    types_mod.InlineKeyboardMarkup = _DummyMarkup
    types_mod.InlineKeyboardButton = _DummyButton
    telebot_mod.types = types.SimpleNamespace(
        InlineKeyboardMarkup=_DummyMarkup,
        InlineKeyboardButton=_DummyButton,
    )

    sys.modules.setdefault("telebot", telebot_mod)
    sys.modules.setdefault("telebot.types", types_mod)


@pytest.fixture(scope="session", autouse=True)
def stub_telebot():
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    os.environ.setdefault("BOT_TOKEN", "1:dummy")
    _install_telebot_stub()
    yield
