from __future__ import annotations

"""Tests for the zerodom-based interaction graph (`page()` / `act()`).

These cover the pure parts (command parsing, compact diff, node extraction
from a zerodom graph) without any network or browser. The live-browser path
is exercised in tests/e2e.
"""

import json

import pytest

from ceki_sdk._browser import _diff_compact, _graph_nodes

# Minimal InteractionGraph-like object standing in for a parsed zerodom graph
# (to_json() returns the node list; to_compact_text() the text).
class _FakeGraph:
    def __init__(self, nodes: list[dict], compact: str):
        self._nodes = nodes
        self._compact = compact

    def to_json(self) -> str:
        return json.dumps({"nodes": self._nodes, "metadata": {"parsing_latency_ms": 3}})

    def to_compact_text(self) -> str:
        return self._compact


def test_graph_nodes_extracts_ids():
    g = _FakeGraph(
        [
            {"id": "node_01", "type": "a", "role": "link", "label": "Home",
             "selector": "body > a", "action": "click", "href": "/"},
            {"id": "node_02", "type": "input", "role": "textbox", "label": "q",
             "selector": "input[name='q']", "action": "fill", "href": None},
        ],
        "[01] a 'Home'\n[02] input 'q' empty",
    )
    nodes = _graph_nodes(g)
    assert len(nodes) == 2
    assert nodes[0]["id"] == "node_01"
    assert nodes[1]["action"] == "fill"


def test_graph_nodes_tolerates_bad_json():
    class Bad:
        def to_json(self) -> str:
            return "not json"

    assert _graph_nodes(Bad()) == []


def test_diff_compact_empty_when_identical():
    text = "[01] a 'Home'\n[02] input 'q'"
    assert _diff_compact(text, text) == ""


def test_diff_compact_shows_changes():
    before = "[01] a 'Home'\n[02] input 'Search'\n[03] a 'About'"
    after = "[01] a 'Home'\n[02] input 'Unlabelled Element'\n[03] a 'About'"
    diff = _diff_compact(before, after)
    assert "Unlabelled" in diff
    assert "-" in diff
    assert "+" in diff


def test_diff_compact_ignores_header():
    b = "PAGE: X | about:blank\n[01] a 'Home'"
    a = "PAGE: X | about:blank\n[01] a 'Home'"
    assert _diff_compact(b, a) == ""