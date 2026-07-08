"""Parser enumeracji urządzeń dshow — czysty, działa na każdym OS."""

from __future__ import annotations

from mediaforge.core.engines.dshow_devices import parse_dshow_audio_devices

SAMPLE = r"""[dshow @ 0] "Integrated Webcam" (video)
[dshow @ 0]   Alternative name "@device_pnp_\\?\usb#vid_0bda"
[dshow @ 0] "Mikrofon (Realtek(R) Audio)" (audio)
[dshow @ 0]   Alternative name "@device_cm_{33D9A762}\Mikrofon (Realtek(R) Audio)"
[dshow @ 0] "Miks stereo (Realtek(R) Audio)" (audio)
[dshow @ 0]   Alternative name "@device_cm_{33D9A762}\Miks stereo (Realtek(R) Audio)"
[dshow @ 0] "CABLE Output (VB-Audio Virtual Cable)" (audio)
[dshow @ 0]   Alternative name "@device_cm_{33D9A762}\CABLE Output"
"""


def test_only_audio_parsed() -> None:
    assert len(parse_dshow_audio_devices(SAMPLE)) == 3  # wideo pominięte


def test_loopback_pl_and_vbcable() -> None:
    d = {x.name: x for x in parse_dshow_audio_devices(SAMPLE)}
    assert d["Mikrofon (Realtek(R) Audio)"].is_loopback is False
    assert d["Miks stereo (Realtek(R) Audio)"].is_loopback is True
    assert d["CABLE Output (VB-Audio Virtual Cable)"].is_loopback is True


def test_alt_name_attached() -> None:
    d = {x.name: x for x in parse_dshow_audio_devices(SAMPLE)}
    assert d["Miks stereo (Realtek(R) Audio)"].alt_name.startswith("@device_cm_")


def test_empty_no_crash() -> None:
    assert parse_dshow_audio_devices("") == []


def test_parser_survives_replacement_chars() -> None:
    """Źle zdekodowane bajty (errors='replace' → U+FFFD) nie wywalają parsera."""
    sample = (
        '[dshow @ 0] "Miks stereo (Realtek�� Audio)" (audio)\n'
        '[dshow @ 0]   Alternative name "@device_cm_{X}\\Miks stereo"\n'
        '[dshow @ 0] "���" (audio)\n'
    )
    devices = parse_dshow_audio_devices(sample)
    assert len(devices) == 2  # obie nazwy sparsowane, mimo krzaków
    # Nazwa z U+FFFD zachowana; loopback wykryty po „miks stereo", alt_name podłączony.
    assert devices[0].name.startswith("Miks stereo")
    assert devices[0].is_loopback is True
    assert devices[0].alt_name.startswith("@device_cm_")
    # Sama „krzaczasta" nazwa też przechodzi (nie loopback, bez alt_name).
    assert devices[1].name == "���"
    assert devices[1].is_loopback is False
    assert devices[1].alt_name == ""
