"""Routing streszczeń: twarda granica prywatności (fail-safe domyślnie-lokalne).

resolve_route WYBIERA trasę, assert_route_allowed to OSTATNIA linia obrony — obie
egzekwują tę samą regułę: bez jawnej zgody materiał nie wychodzi do chmury.
"""

from __future__ import annotations

import pytest

from mediaforge.core.ai.routing import (
    ModelRoute,
    RouteKind,
    SensitivityViolation,
    assert_route_allowed,
    resolve_route,
)


def test_no_cloud_ok_stays_local_despite_cloud_model() -> None:
    """Brak zgody (cloud_ok=False) → trasa lokalna, MIMO skonfigurowanego modelu chmurowego."""
    route = resolve_route(
        cloud_ok=False, local_model="ollama/qwen3", cloud_model="anthropic/claude"
    )
    assert route == ModelRoute(RouteKind.LOCAL, "ollama/qwen3")
    assert not route.is_cloud


def test_cloud_ok_with_cloud_model_routes_cloud() -> None:
    """Zgoda + skonfigurowany model chmurowy → trasa chmurowa."""
    route = resolve_route(cloud_ok=True, local_model="ollama/qwen3", cloud_model="anthropic/claude")
    assert route == ModelRoute(RouteKind.CLOUD, "anthropic/claude")
    assert route.is_cloud


def test_cloud_ok_without_cloud_model_falls_back_local() -> None:
    """Zgoda, ale brak modelu chmurowego (None) → trasa lokalna (brak trasy chmurowej)."""
    route = resolve_route(cloud_ok=True, local_model="ollama/qwen3", cloud_model=None)
    assert route == ModelRoute(RouteKind.LOCAL, "ollama/qwen3")


def test_assert_blocks_sensitive_to_cloud() -> None:
    """Ostatnia linia obrony: wrażliwy materiał (cloud_ok=False) na trasie chmurowej → wyjątek."""
    cloud_route = ModelRoute(RouteKind.CLOUD, "anthropic/claude")
    with pytest.raises(SensitivityViolation):
        assert_route_allowed(cloud_route, cloud_ok=False)


def test_assert_allows_cloud_with_consent_and_local_always() -> None:
    """Trasa chmurowa za zgodą i trasa lokalna zawsze — nie rzucają."""
    assert_route_allowed(ModelRoute(RouteKind.CLOUD, "anthropic/claude"), cloud_ok=True)
    assert_route_allowed(ModelRoute(RouteKind.LOCAL, "ollama/qwen3"), cloud_ok=False)
    # Bez modelu lokalnego i bez dozwolonej chmury — błąd konfiguracji (nie ma czym streszczać).
    with pytest.raises(ValueError, match="lokalnego"):
        resolve_route(cloud_ok=False, local_model=None, cloud_model="anthropic/claude")
