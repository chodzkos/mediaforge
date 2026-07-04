"""Edytor profili źródeł — prosta lista domen + pola domyślnych metadanych.

Profil (per domena) niesie domyślne kategorię/tagi/organizatora + ``cloud_ok`` nowych
materiałów z tego źródła. Zgoda na chmurę jest tu ustawiana ŚWIADOMIE przez użytkownika —
tylko to może podnieść domyślny ``cloud_ok`` ponad globalny fail-safe (False).
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mediaforge.core import config as cfg_mod
from mediaforge.core.library.db import Database
from mediaforge.core.library.profiles import SourceProfile, SourceProfileStore


class ProfilesDialog(QDialog):
    """Lista profili domen + edycja/zapis/usuwanie (kategoria, tagi, organizator, cloud_ok)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Profile źródeł")
        self.setMinimumWidth(560)

        db_path = cfg_mod.library_db_path()
        Database(db_path).migrate()
        self._store = SourceProfileStore(db_path)
        self._build_ui()
        self._reload()

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)

        self._list = QListWidget()
        self._list.setMinimumWidth(200)
        self._list.currentRowChanged.connect(self._on_select)
        root.addWidget(self._list)

        right = QVBoxLayout()
        form = QFormLayout()
        self._domain = QLineEdit()
        self._domain.setPlaceholderText("np. konferencja.example.com")
        form.addRow("Domena:", self._domain)
        self._category = QLineEdit()
        form.addRow("Kategoria:", self._category)
        self._organizer = QLineEdit()
        form.addRow("Organizator:", self._organizer)
        self._tags = QLineEdit()
        self._tags.setPlaceholderText("tagi po przecinku")
        form.addRow("Tagi:", self._tags)
        self._cloud_ok = QCheckBox("Domyślnie zezwól na chmurę dla tego źródła")
        self._cloud_ok.setToolTip("Podnosi domyślny cloud_ok nowych materiałów (świadoma zgoda)")
        form.addRow("Prywatność:", self._cloud_ok)
        right.addLayout(form)

        actions = QHBoxLayout()
        new_btn = QPushButton("Nowy")
        new_btn.clicked.connect(self._on_new)
        actions.addWidget(new_btn)
        save_btn = QPushButton("Zapisz")
        save_btn.clicked.connect(self._on_save)
        actions.addWidget(save_btn)
        del_btn = QPushButton("Usuń")
        del_btn.clicked.connect(self._on_delete)
        actions.addWidget(del_btn)
        actions.addStretch(1)
        right.addLayout(actions)
        right.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.reject)
        right.addWidget(buttons)
        root.addLayout(right, stretch=1)

    def _reload(self) -> None:
        self._profiles = self._store.list_all()
        self._list.clear()
        for profile in self._profiles:
            self._list.addItem(profile.domain)

    def _on_select(self, row: int) -> None:
        if not (0 <= row < len(self._profiles)):
            return
        profile = self._profiles[row]
        self._domain.setText(profile.domain)
        self._category.setText(profile.category or "")
        self._organizer.setText(profile.organizer or "")
        self._tags.setText(", ".join(profile.tags))
        self._cloud_ok.setChecked(profile.cloud_ok)

    def _on_new(self) -> None:
        for edit in (self._domain, self._category, self._organizer, self._tags):
            edit.clear()
        self._cloud_ok.setChecked(False)
        self._list.setCurrentRow(-1)  # brak zaznaczenia — czysty formularz nowego profilu

    def _current_profile(self) -> SourceProfile | None:
        domain = self._domain.text().strip().lower()
        if not domain:
            return None
        return SourceProfile(
            domain=domain,
            category=self._category.text().strip() or None,
            tags=tuple(t.strip() for t in self._tags.text().split(",") if t.strip()),
            organizer=self._organizer.text().strip() or None,
            cloud_ok=self._cloud_ok.isChecked(),
        )

    def _on_save(self) -> None:
        profile = self._current_profile()
        if profile is not None:
            self._store.upsert(profile)
            self._reload()

    def _on_delete(self) -> None:
        domain = self._domain.text().strip().lower()
        if domain:
            self._store.delete(domain)
            self._on_new()
            self._reload()
