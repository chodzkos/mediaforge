"""Routing modelu streszczenia z TWARDĄ granicą prywatności (Qt-free).

Reguła fail-safe (domyślnie-lokalne): materiał jest wrażliwy, DOPÓKI użytkownik
jawnie nie ustawi ``cloud_ok=True``. Brak zgody = przetwarzanie WYŁĄCZNIE lokalnie.

Egzekucja jest dwustopniowa i celowo redundantna:

* :func:`resolve_route` **wybiera** trasę (lokalną albo chmurową) na podstawie zgody
  i dostępności skonfigurowanych modeli — nigdy nie zwróci trasy chmurowej dla materiału
  bez zgody;
* :func:`assert_route_allowed` to **ostatnia linia obrony** tuż przed wysyłką: rzuca
  :class:`SensitivityViolation`, gdy wrażliwy materiał (``cloud_ok=False``) miałby trafić
  trasą chmurową. Sprawdza tę samą regułę drugi raz — gdyby wybór trasy kiedykolwiek
  rozjechał się z polityką (bug, ręczna podmiana), materiał i tak nie wyjdzie do chmury.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RouteKind(StrEnum):
    """Tor przetwarzania streszczenia."""

    LOCAL = "local"  # model lokalny (Ollama przez gateway) — zawsze dozwolony
    CLOUD = "cloud"  # model chmurowy (przez gateway) — tylko za jawną zgodą


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """Rozstrzygnięta trasa: który model i którym torem (lokalny/chmurowy)."""

    kind: RouteKind
    model: str

    @property
    def is_cloud(self) -> bool:
        """Czy trasa prowadzi do modelu chmurowego (wymaga zgody ``cloud_ok``)."""
        return self.kind is RouteKind.CLOUD


class SensitivityViolation(Exception):
    """Próba wysłania wrażliwego materiału (``cloud_ok=False``) trasą chmurową.

    Sygnalizuje naruszenie twardej granicy prywatności — łapana przez handler i
    zamieniana na czytelny błąd zadania, NIGDY nie prowadzi do wysyłki do chmury.
    """


def resolve_route(
    *,
    cloud_ok: bool,
    local_model: str | None,
    cloud_model: str | None,
) -> ModelRoute:
    """Wybiera trasę streszczenia wg zgody na chmurę i dostępnych modeli.

    Reguła (fail-safe):

    * ``cloud_ok=False`` → ZAWSZE trasa lokalna, nawet gdy skonfigurowano model chmurowy
      (zapomnienie zgody jest bezpieczne — materiał zostaje lokalnie);
    * ``cloud_ok=True`` i jest ``cloud_model`` → trasa chmurowa;
    * ``cloud_ok=True`` bez ``cloud_model`` → trasa lokalna (brak skonfigurowanej chmury).

    Trasa lokalna wymaga ``local_model`` — jego brak to błąd konfiguracji
    (``ValueError``: nie ma czym streszczać lokalnie, a do chmury nie wolno).
    """
    if cloud_ok and cloud_model:
        return ModelRoute(RouteKind.CLOUD, cloud_model)
    if not local_model:
        raise ValueError(
            "Brak skonfigurowanego modelu lokalnego (summary_model_local) — "
            "ustaw go w konfiguracji; do chmury bez zgody nie wolno."
        )
    return ModelRoute(RouteKind.LOCAL, local_model)


def assert_route_allowed(route: ModelRoute, *, cloud_ok: bool) -> None:
    """Ostatnia linia obrony: blokuje trasę chmurową dla materiału bez zgody.

    Wołane tuż przed budową i wysłaniem żądania. Gdy ``route.is_cloud`` a ``cloud_ok``
    jest ``False`` — rzuca :class:`SensitivityViolation` (materiał NIE wychodzi do chmury).
    Trasa lokalna jest zawsze dozwolona.
    """
    if route.is_cloud and not cloud_ok:
        raise SensitivityViolation(
            "Materiał bez zgody na przetwarzanie w chmurze (cloud_ok=False) — "
            "trasa chmurowa zablokowana. Przetwarzaj lokalnie albo zaznacz zgodę."
        )
