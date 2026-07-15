"""Klient VLM: kształt requestu (content-lista, data-URL, MIME) i parser odpowiedzi na 3 pola."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from mediaforge.core.ai.gateway import GatewayError
from mediaforge.core.ai.routing import ModelRoute, RouteKind
from mediaforge.core.ai.vision import (
    SlideAnalysis,
    VisionClient,
    VisionConfig,
    build_vision_request,
    parse_slide_analysis,
)

_ROUTE = ModelRoute(RouteKind.LOCAL, "ollama/qwen-vl-local")
_PNG_BYTES = b"\x89PNG\r\n\x1a\n_fake_png_payload_"


def _image(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(_PNG_BYTES)
    return path


def test_build_request_content_list_with_data_url(tmp_path: Path) -> None:
    """Payload vision: model z trasy, content-lista tekst+image_url, data-URL base64."""
    img = _image(tmp_path, "slajd_1.png")
    cfg = VisionConfig(base_url="http://gw:4000")
    req = build_vision_request(img, "PROMPT", _ROUTE, cfg)

    assert req["model"] == "ollama/qwen-vl-local"
    assert req["max_tokens"] == 2048  # domyślny budżet VLM (korekta pre-flight)
    system, user = req["messages"]
    assert system["role"] == "system"
    content = user["content"]
    assert content[0] == {"type": "text", "text": "PROMPT"}
    url = content[1]["image_url"]["url"]
    expected_b64 = base64.b64encode(_PNG_BYTES).decode("ascii")
    assert url == f"data:image/png;base64,{expected_b64}"


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("s.png", "image/png"),
        ("s.jpg", "image/jpeg"),
        ("s.jpeg", "image/jpeg"),
        ("s.webp", "image/webp"),
        ("s.PNG", "image/png"),  # rozszerzenie case-insensitive
        ("s.bin", "image/png"),  # nieznane → PNG (bezpieczny domyślny)
    ],
)
def test_mime_from_extension(tmp_path: Path, name: str, mime: str) -> None:
    """MIME data-URL wybierany z rozszerzenia pliku (png/jpeg/webp; nieznane → png)."""
    img = _image(tmp_path, name)
    req = build_vision_request(img, "P", _ROUTE, VisionConfig(base_url="x"))
    url = req["messages"][1]["content"][1]["image_url"]["url"]
    assert url.startswith(f"data:{mime};base64,")


def test_build_request_includes_prompt_suffix(tmp_path: Path) -> None:
    """System-prompt niesie sufiks (domyślnie ``/no_think`` dla qwen3-vl); pusty = brak dopisku."""
    img = _image(tmp_path, "s.png")
    default = build_vision_request(img, "P", _ROUTE, VisionConfig(base_url="x"))
    assert default["messages"][0]["content"].endswith("/no_think")
    cleared = build_vision_request(img, "P", _ROUTE, VisionConfig(base_url="x", prompt_suffix=""))
    assert "/no_think" not in cleared["messages"][0]["content"]


def test_parse_full_response() -> None:
    """Pełna odpowiedź TYTUŁ/TEKST/OPIS → trzy pola wypełnione (wielolinijkowy TEKST sklejony)."""
    raw = "TYTUŁ:\nWprowadzenie\nTEKST:\nLinia 1\nLinia 2\nOPIS:\nWykres słupkowy."
    result = parse_slide_analysis(raw)
    assert result == SlideAnalysis(
        title="Wprowadzenie", text="Linia 1\nLinia 2", description="Wykres słupkowy."
    )


def test_parse_inline_header_values() -> None:
    """Wartość w tej samej linii co nagłówek (``TYTUŁ: X``) też jest odczytana."""
    raw = "TYTUŁ: Krótki\nTEKST: jedno zdanie\nOPIS: schemat"
    result = parse_slide_analysis(raw)
    assert result == SlideAnalysis(title="Krótki", text="jedno zdanie", description="schemat")


def test_parse_missing_sections_yield_empty_not_crash() -> None:
    """Brak sekcji → puste pole (nie crash). Tu brak OPIS i brak TEKST."""
    result = parse_slide_analysis("TYTUŁ:\nSam tytuł")
    assert result == SlideAnalysis(title="Sam tytuł", text="", description="")
    # Zupełnie niesformatowana odpowiedź (brak nagłówków) → wszystkie pola puste.
    assert parse_slide_analysis("losowy tekst bez nagłówków") == SlideAnalysis("", "", "")


def test_analyze_uses_gateway_and_returns_content(tmp_path: Path) -> None:
    """``analyze`` buduje payload z obrazem, POST-uje przez transport i zwraca treść odpowiedzi."""
    img = _image(tmp_path, "s.png")
    captured: dict[str, object] = {}

    def transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> bytes:
        captured["payload"] = json.loads(body)
        captured["url"] = url
        return json.dumps(
            {"choices": [{"message": {"content": "TYTUŁ:\nA\nTEKST:\nB\nOPIS:\nC"}}]}
        ).encode("utf-8")

    client = VisionClient(VisionConfig(base_url="http://gw:4000"), transport=transport)
    analysis = client.analyze_slide(img, _ROUTE)

    assert captured["url"] == "http://gw:4000/v1/chat/completions"
    assert analysis == SlideAnalysis(title="A", text="B", description="C")
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["messages"][1]["content"][1]["type"] == "image_url"


def test_analyze_empty_budget_exhausted_raises(tmp_path: Path) -> None:
    """Pusta treść + completion_tokens >= max_tokens → GatewayError (qwen3-vl zjadł budżet)."""
    img = _image(tmp_path, "s.png")

    def transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> bytes:
        return json.dumps(
            {
                "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 2048},
            }
        ).encode("utf-8")

    client = VisionClient(VisionConfig(base_url="http://gw:4000", max_tokens=2048), transport)
    with pytest.raises(GatewayError, match="limit tokenów"):
        client.analyze(img, _ROUTE)
