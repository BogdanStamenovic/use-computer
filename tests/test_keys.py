from __future__ import annotations

import pytest

from use_computer import UseComputerError
from use_computer.keys import Segment, char_keysym, parse_combo, parse_keys, segment_text


def test_combo_order_and_names() -> None:
    assert parse_combo("ctrl+shift+t") == [0xFFE3, 0xFFE1, ord("t")]
    assert parse_combo("Return") == [0xFF0D]
    assert parse_combo("super") == [0xFFEB]
    assert parse_combo("F12") == [0xFFC9]


def test_literal_plus() -> None:
    assert parse_combo("ctrl+plus") == [0xFFE3, ord("+")]
    assert parse_combo("ctrl++") == [0xFFE3, ord("+")]


def test_sequence() -> None:
    assert parse_keys("Tab Tab Return") == [[0xFF09], [0xFF09], [0xFF0D]]


def test_unknown_key_is_an_error() -> None:
    with pytest.raises(UseComputerError):
        parse_combo("ctrl+hyperdrive")
    with pytest.raises(UseComputerError):
        parse_combo("š")


def test_char_keysym() -> None:
    assert char_keysym("A") == 0x41
    assert char_keysym("\n") == 0xFF0D
    assert char_keysym("š") is None


def test_segments_split_on_typeability() -> None:
    assert segment_text("Hi šđ ok") == [Segment("keys", "Hi "), Segment("paste", "šđ"),
                                         Segment("keys", " ok")]
    assert segment_text("") == []
