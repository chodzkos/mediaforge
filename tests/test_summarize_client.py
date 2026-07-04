"""Klient streszczeń: kształt requestu, normalizacja URL, parsowanie odpowiedzi gatewaya."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from mediaforge.core.ai.routing import ModelRoute, RouteKind
from mediaforge.core.ai.summarize import (
    GatewayError,
    SummaryClient,
    SummaryConfig,
    build_summary_request,
    parse_summary_response,
)

_ROUTE = ModelRoute(RouteKind.LOCAL, "ollama/qwen3:27b")


def test_build_request_shape() -> None:
    """Payload chat-completions: model z trasy, system+user, max_tokens, język w system-prompcie."""
    req = build_summary_request(
        "PEŁNY TRANSKRYPT",
        ModelRoute(RouteKind.CLOUD, "anthropic/claude-3"),
        SummaryConfig(base_url="http://gw:4000", language="pl", max_tokens=500),
    )
    assert req["model"] == "anthropic/claude-3"
    assert req["max_tokens"] == 500
    system, user = req["messages"]
    assert system["role"] == "system" and "pl" in system["content"]
    assert user == {"role": "user", "content": "PEŁNY TRANSKRYPT"}


def test_build_request_includes_prompt_suffix() -> None:
    """System-prompt niesie sufiks (domyślnie ``/no_think`` dla qwen3); pusty = brak dopisku."""
    default = build_summary_request(
        "T", ModelRoute(RouteKind.LOCAL, "ollama/qwen3"), SummaryConfig(base_url="x")
    )
    assert default["messages"][0]["content"].endswith("/no_think")
    cleared = build_summary_request(
        "T", ModelRoute(RouteKind.LOCAL, "m"), SummaryConfig(base_url="x", prompt_suffix="")
    )
    assert "/no_think" not in cleared["messages"][0]["content"]


def test_parse_empty_content_budget_exhausted() -> None:
    """Pusty content + completion_tokens >= max_tokens → komunikat o limicie na rozumowanie."""
    data = {
        # finish_reason „stop" MYLI (raportowany mimo ucięcia) — diagnostyki na nim nie opieramy.
        "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
        "usage": {"completion_tokens": 4096},
    }
    with pytest.raises(GatewayError, match="limit tokenów"):
        parse_summary_response(data, max_tokens=4096)


def test_parse_empty_content_without_budget_exhausted() -> None:
    """Pusty content, ale budżet NIE wyczerpany → dotychczasowy komunikat „pusta treść"."""
    data = {"choices": [{"message": {"content": "   "}}], "usage": {"completion_tokens": 12}}
    with pytest.raises(GatewayError, match="pustą treść"):
        parse_summary_response(data, max_tokens=4096)
    # Bez informacji o usage też komunikat ogólny (nie ma na czym oprzeć diagnozy budżetu).
    with pytest.raises(GatewayError, match="pustą treść"):
        parse_summary_response({"choices": [{"message": {"content": ""}}]}, max_tokens=4096)


def test_endpoint_trailing_slash_normalized() -> None:
    """base_url z i bez trailing slash daje ten sam endpoint (brak podwójnego //)."""
    captured: dict[str, str] = {}

    def transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> bytes:
        captured["url"] = url
        return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

    for base in ("http://gw:4000", "http://gw:4000/"):
        client = SummaryClient(SummaryConfig(base_url=base), transport=transport)
        client.summarize("t", _ROUTE)
        assert captured["url"] == "http://gw:4000/v1/chat/completions"


def test_parse_ok() -> None:
    """Poprawna odpowiedź → wyciągnięta (i przycięta) treść."""
    data = {"choices": [{"message": {"role": "assistant", "content": "  # Streszczenie\n…  "}}]}
    assert parse_summary_response(data) == "# Streszczenie\n…"


def test_parse_gateway_error_field() -> None:
    """Odpowiedź z polem ``error`` → GatewayError (gateway zgłosił problem)."""
    with pytest.raises(GatewayError, match="błąd"):
        parse_summary_response({"error": {"message": "model not found", "code": 404}})


def test_parse_garbage_and_empty() -> None:
    """Śmieci/niepoprawny kształt oraz pusta treść → GatewayError."""
    bad_inputs: tuple[object, ...] = (
        [],
        "nie-mapa",
        {},
        {"choices": []},
        {"choices": [{"message": {"content": "   "}}]},
    )
    for bad in bad_inputs:
        with pytest.raises(GatewayError):
            parse_summary_response(bad)
