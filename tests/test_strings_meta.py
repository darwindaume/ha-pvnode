"""Device naming for roof surfaces."""

from __future__ import annotations

from custom_components.pvnode.strings_meta import build_string_names, compass_point


def test_compass_points_follow_the_language() -> None:
    """East is O in German and E in English; getting that wrong looks machine-made."""
    assert compass_point(90, "de") == "O"
    assert compass_point(90, "en") == "E"
    assert compass_point(180, "de") == "S"
    assert compass_point(315, "en") == "NW"
    # Unknown languages fall back rather than raising.
    assert compass_point(90, "fr") == "E"


def test_distinct_surfaces_need_no_position() -> None:
    """A marker on every device would be noise where the geometry already differs."""
    site = {
        "strings": [
            {"slope": 30.0, "orientation": 180.0, "power_kw": 9.9},
            {"slope": 30.0, "orientation": 90.0, "power_kw": 9.9},
        ]
    }
    names = build_string_names(site, "de")
    assert "position" not in names[0]
    assert "position" not in names[1]


def test_identical_surfaces_get_a_position() -> None:
    """A split array on one roof face is otherwise indistinguishable by name."""
    site = {
        "strings": [
            {"slope": 30.0, "orientation": 180.0, "power_kw": 5.0},
            {"slope": 30.0, "orientation": 180.0, "power_kw": 5.0},
            {"slope": 45.0, "orientation": 180.0, "power_kw": 5.0},
        ]
    }
    names = build_string_names(site, "de")
    assert names[0]["position"] == "1"
    assert names[1]["position"] == "2"
    # The third differs in tilt, so it stands on its own.
    assert "position" not in names[2]


def test_unusable_geometry_is_skipped() -> None:
    """A string without usable numbers falls back to the positional device name."""
    site = {"strings": [{"slope": None, "orientation": 180.0, "power_kw": 5.0}]}
    assert build_string_names(site, "de") == {}
