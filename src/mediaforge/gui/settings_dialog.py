"""Dialog Ustawień AI — edycja kluczy configu (gateway, streszczenia, notatki VLM, nagrywanie).

Koniec ręcznego ``uv run python -c "c.set(...)"`` w terminalu: wszystkie klucze, które
użytkownik bez znajomości kodu MUSI móc zmienić, mają widget. Klucze wewnętrzne (np.
``summary_prompt_suffix``) świadomie zostają w configu — nie każdy klucz potrzebuje pola.

Zapis przez obiekt ``Config`` (kitowy magazyn): puste pole tekstowe / spinbox = 0 → ``None`` →
getter w kodzie zwraca default. Po zapisie wołany jest ``on_saved`` (właściciel re-rejestruje
handlery z nowym configiem → zmiany działają od NASTĘPNEGO zadania, bez restartu aplikacji).

Walidacja „Sprawdź": sonda gatewaya (``check_litellm`` z doctora) w wątku roboczym → sprawdzenie,
czy wpisana nazwa modelu jest wśród modeli gatewaya. Wynik inline (✓/✗ + lista), bez crasha przy
martwym gatewayu. Czyste funkcje :func:`gateway_summary`/:func:`validate_model` są testowalne bez
sieci.
"""

from __future__ import annotations

from collections.abc import Callable

from chodzkos_gui_kit.config import Config
from chodzkos_gui_kit.qt.theme import current_palette
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from mediaforge.core import config as cfg_mod
from mediaforge.core.detection import tools as detect_tools

_DEFAULT_BASE_URL = "http://localhost:4000"


def gateway_summary(available: bool, models: list[str]) -> tuple[bool, str]:
    """Wynik sprawdzenia samego gatewaya (pole URL): osiągalność + liczba/lista modeli."""
    if not available:
        return False, "✗ gateway niedostępny — sprawdź URL i czy proces LiteLLM działa"
    listed = ", ".join(models) if models else "(brak modeli w configu gatewaya)"
    return True, f"✓ gateway odpowiada — {len(models)} modeli: {listed}"


def validate_model(available: bool, models: list[str], model: str) -> tuple[bool, str]:
    """Czy ``model`` istnieje na gatewayu. Pusty model / martwy gateway → czytelny komunikat."""
    if not available:
        return False, "✗ gateway niedostępny — sprawdź URL i czy proces LiteLLM działa"
    if not model:
        return False, "✗ wpisz nazwę modelu"
    if model in models:
        return True, "✓ model dostępny na gatewayu"
    listed = ", ".join(models) if models else "(brak)"
    return False, f"✗ brak na gatewayu — dostępne: {listed}"


class _GatewayProbe(QThread):
    """Sonda gatewaya (``/v1/models``) w wątku roboczym — HTTP nie blokuje wątku UI."""

    probed = Signal(bool, list)  # (available, models: list[str])

    def __init__(self, base_url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._base_url = base_url

    def run(self) -> None:
        report = detect_tools.check_litellm(self._base_url or _DEFAULT_BASE_URL)
        self.probed.emit(bool(report.get("available")), list(report.get("models") or []))


class SettingsDialog(QDialog):
    """Ustawienia AI zapisywane przez ``Config``. ``on_saved`` woła się po każdym udanym zapisie."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        config: Config,
        on_saved: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ustawienia")
        self.setMinimumWidth(560)
        self._config = config
        self._on_saved = on_saved
        self._probe: _GatewayProbe | None = None
        self._pending: str | None = None  # id pola, dla którego trwa „Sprawdź"
        self._result_labels: dict[str, QLabel] = {}
        self._model_edits: dict[str, QLineEdit] = {}
        self._build_ui()
        self._load()

    # ── Budowa UI ─────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.addWidget(self._gateway_group())
        root.addWidget(self._summaries_group())
        root.addWidget(self._notes_group())
        root.addWidget(self._recording_group())

        note = QLabel(
            "Zmiany modeli/gatewaya działają od następnego zadania (bez restartu aplikacji)."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {current_palette().fg3};")
        root.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        apply_btn = buttons.button(QDialogButtonBox.StandardButton.Apply)
        if apply_btn is not None:
            apply_btn.clicked.connect(self._save)
        root.addWidget(buttons)

    def _gateway_group(self) -> QGroupBox:
        box = QGroupBox("Gateway")
        form = QFormLayout(box)
        self._base_url = QLineEdit()
        self._base_url.setPlaceholderText(_DEFAULT_BASE_URL)
        form.addRow("Adres gatewaya (LiteLLM):", self._model_row(self._base_url, "base_url"))
        form.addRow("", self._result_labels["base_url"])
        return box

    def _summaries_group(self) -> QGroupBox:
        box = QGroupBox("Streszczenia")
        form = QFormLayout(box)
        self._summary_local = QLineEdit()
        self._summary_local.setPlaceholderText("np. ollama/qwen3:27b")
        form.addRow("Model lokalny:", self._model_row(self._summary_local, "summary_local"))
        form.addRow("", self._result_labels["summary_local"])

        self._summary_cloud = QLineEdit()
        self._summary_cloud.setPlaceholderText("puste = brak trasy chmurowej")
        form.addRow("Model chmurowy:", self._model_row(self._summary_cloud, "summary_cloud"))
        form.addRow("", self._result_labels["summary_cloud"])

        self._summary_timeout = _spin(0, 7200, cfg_mod.DEFAULT_SUMMARY_TIMEOUT_SEC, suffix=" s")
        form.addRow("Timeout (s):", self._summary_timeout)
        self._summary_max_tokens = _spin(0, 32768, cfg_mod.DEFAULT_SUMMARY_MAX_TOKENS)
        form.addRow("Limit tokenów:", self._summary_max_tokens)
        self._summary_reduce = _spin(0, 32768, cfg_mod.DEFAULT_SUMMARY_REDUCE_MAX_TOKENS)
        form.addRow("Limit tokenów (reduce):", self._summary_reduce)
        self._summary_chunk = _spin(0, 500000, cfg_mod.DEFAULT_SUMMARY_CHUNK_CHARS, step=1000)
        form.addRow("Próg podziału (znaki):", self._summary_chunk)
        return box

    def _notes_group(self) -> QGroupBox:
        box = QGroupBox("Notatki (VLM)")
        form = QFormLayout(box)
        self._vlm_local = QLineEdit()
        self._vlm_local.setPlaceholderText("np. ollama/qwen-vl-local")
        form.addRow("Model lokalny:", self._model_row(self._vlm_local, "vlm_local"))
        form.addRow("", self._result_labels["vlm_local"])

        self._vlm_cloud = QLineEdit()
        self._vlm_cloud.setPlaceholderText("puste = brak trasy chmurowej")
        form.addRow("Model chmurowy:", self._model_row(self._vlm_cloud, "vlm_cloud"))
        form.addRow("", self._result_labels["vlm_cloud"])

        self._vlm_max_tokens = _spin(0, 32768, cfg_mod.DEFAULT_VLM_MAX_TOKENS)
        form.addRow("Limit tokenów:", self._vlm_max_tokens)
        return box

    def _recording_group(self) -> QGroupBox:
        box = QGroupBox("Nagrywanie")
        form = QFormLayout(box)
        # 0 to poprawna wartość (bez pre-rolla) — bez „domyślnie" jako specjalnej wartości.
        self._preroll = QSpinBox()
        self._preroll.setRange(0, 60)
        self._preroll.setSuffix(" s")
        form.addRow("Pre-roll przed nagraniem:", self._preroll)
        return box

    def _model_row(self, edit: QLineEdit, field_id: str) -> QWidget:
        """Wiersz: pole + „Sprawdź"; rejestruje pole i etykietę wyniku pod ``field_id``."""
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit)
        button = QPushButton("Sprawdź")
        button.clicked.connect(lambda: self._check(field_id))
        row.addWidget(button)
        self._model_edits[field_id] = edit
        self._result_labels[field_id] = QLabel("")
        self._result_labels[field_id].setWordWrap(True)
        return wrapper

    # ── Ładowanie / zapis ─────────────────────────────────────────────────────

    def _load(self) -> None:
        c = self._config
        self._base_url.setText(cfg_mod.get_litellm_base_url(c) or "")
        self._summary_local.setText(cfg_mod.get_summary_model_local(c) or "")
        self._summary_cloud.setText(cfg_mod.get_summary_model_cloud(c) or "")
        self._summary_timeout.setValue(cfg_mod.get_summary_timeout_override(c) or 0)
        self._summary_max_tokens.setValue(cfg_mod.get_summary_max_tokens_override(c) or 0)
        self._summary_reduce.setValue(cfg_mod.get_summary_reduce_max_tokens_override(c) or 0)
        self._summary_chunk.setValue(cfg_mod.get_summary_chunk_chars_override(c) or 0)
        self._vlm_local.setText(cfg_mod.get_vlm_model_local(c) or "")
        self._vlm_cloud.setText(cfg_mod.get_vlm_model_cloud(c) or "")
        self._vlm_max_tokens.setValue(cfg_mod.get_vlm_max_tokens_override(c) or 0)
        self._preroll.setValue(cfg_mod.get_record_preroll_sec(c))

    def _save(self) -> None:
        c = self._config
        cfg_mod.set_litellm_base_url(c, self._base_url.text().strip() or None)
        cfg_mod.set_summary_model_local(c, self._summary_local.text().strip() or None)
        cfg_mod.set_summary_model_cloud(c, self._summary_cloud.text().strip() or None)
        cfg_mod.set_summary_timeout(c, _spin_value(self._summary_timeout))
        cfg_mod.set_summary_max_tokens(c, _spin_value(self._summary_max_tokens))
        cfg_mod.set_summary_reduce_max_tokens(c, _spin_value(self._summary_reduce))
        cfg_mod.set_summary_chunk_chars(c, _spin_value(self._summary_chunk))
        cfg_mod.set_vlm_model_local(c, self._vlm_local.text().strip() or None)
        cfg_mod.set_vlm_model_cloud(c, self._vlm_cloud.text().strip() or None)
        cfg_mod.set_vlm_max_tokens(c, _spin_value(self._vlm_max_tokens))
        cfg_mod.set_record_preroll_sec(c, self._preroll.value())
        c.save_now()
        if self._on_saved is not None:
            self._on_saved()

    def _on_ok(self) -> None:
        self._save()
        self.accept()

    # ── „Sprawdź" (walidacja modelu / gatewaya) ────────────────────────────────

    def _check(self, field_id: str) -> None:
        if self._probe is not None and self._probe.isRunning():
            return
        self._pending = field_id
        self._result_labels[field_id].setText("sprawdzam…")
        probe = _GatewayProbe(self._base_url.text().strip(), self)
        probe.probed.connect(self._on_probed)
        self._probe = probe
        probe.start()

    def _on_probed(self, available: bool, models: list[str]) -> None:
        field_id = self._pending
        if field_id is None:
            return
        if field_id == "base_url":
            _ok, message = gateway_summary(available, models)
        else:
            model = self._model_edits[field_id].text().strip()
            _ok, message = validate_model(available, models, model)
        self._result_labels[field_id].setText(message)
        self._pending = None


def _spin(
    minimum: int, maximum: int, default: float, *, suffix: str = "", step: int = 1
) -> QSpinBox:
    """Spinbox z ``specialValueText`` przy 0 = „(domyślnie N)" — 0/pusto → default z kodu."""
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setSingleStep(step)
    if suffix:
        spin.setSuffix(suffix)
    spin.setSpecialValueText(f"(domyślnie {int(default)})")  # widoczne, gdy value == minimum (0)
    return spin


def _spin_value(spin: QSpinBox) -> int | None:
    """Wartość spinboxa albo ``None`` przy 0 (= użyj defaultu z kodu)."""
    value = spin.value()
    return value if value > 0 else None
