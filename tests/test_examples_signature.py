from __future__ import annotations

import importlib
import inspect

import pytest


@pytest.mark.parametrize("module_name", [
    "examples.reddit_signup",
    "examples.github_signup",
    "examples.imap_helper",
])
def test_example_imports(module_name):
    mod = importlib.import_module(module_name)
    assert mod is not None


def test_imap_helper_signature():
    from examples.imap_helper import wait_for_confirm_link

    sig = inspect.signature(wait_for_confirm_link)
    assert "tag" in sig.parameters
    assert "timeout" in sig.parameters
    assert "service" in sig.parameters
