"""Naming for per-string (roof surface) devices.

pvnode's `PVString` carries no name — only geometry. A device called "String 2" tells
the user nothing, so the name is derived from what does distinguish one surface from
another: where it points, how steep it is, and how big it is.
"""

from __future__ import annotations

from typing import Any

# Eight-point compass. Only NE/NO, E/O and SE/SO differ between the two languages,
# but getting those wrong is exactly what makes a name look machine-generated.
_COMPASS: dict[str, tuple[str, ...]] = {
    "de": ("N", "NO", "O", "SO", "S", "SW", "W", "NW"),
    "en": ("N", "NE", "E", "SE", "S", "SW", "W", "NW"),
}


def compass_point(orientation: float, language: str) -> str:
    """Turn an azimuth (0=N, 90=E, 180=S, 270=W) into a compass abbreviation."""
    points = _COMPASS.get(language.split("-")[0].lower(), _COMPASS["en"])
    index = int((orientation % 360) / 45 + 0.5) % 8
    return points[index]


def string_placeholders(string: dict[str, Any], language: str) -> dict[str, str] | None:
    """Build the device-name placeholders for one string, or None if unusable."""
    try:
        orientation = float(string["orientation"])
        slope = float(string["slope"])
        power = float(string["power_kw"])
    except (KeyError, TypeError, ValueError):
        return None

    return {
        "direction": compass_point(orientation, language),
        "slope": f"{slope:g}",
        "power": f"{power:g}",
    }


def build_string_names(
    site: dict[str, Any], language: str
) -> dict[int, dict[str, str]]:
    """Map each string's positional index to its device-name placeholders.

    The index is the position in the site's `strings` array, which is exactly what the
    forecast reports as `string_index`.

    Two surfaces can share bearing, tilt and size — a split array on one roof face is
    the obvious case. Those would otherwise produce the same device name and Home
    Assistant would disambiguate them with a `_2` entity id suffix, which tells the user
    nothing. Only the surfaces that actually clash get a positional marker.
    """
    names: dict[int, dict[str, str]] = {}
    for index, string in enumerate(site.get("strings") or []):
        if placeholders := string_placeholders(string, language):
            names[index] = placeholders

    seen: dict[tuple[str, ...], list[int]] = {}
    for index, placeholders in names.items():
        key = (placeholders["direction"], placeholders["slope"], placeholders["power"])
        seen.setdefault(key, []).append(index)

    for indexes in seen.values():
        if len(indexes) < 2:
            continue
        for position, index in enumerate(sorted(indexes), start=1):
            names[index]["position"] = str(position)

    return names


def string_model(placeholders: dict[str, str] | None) -> str:
    """Device `model` line for a roof surface — the geometry in full."""
    if not placeholders:
        return "pvnode API v2 (String)"
    return (
        f"{placeholders['power']} kWp · {placeholders['direction']} "
        f"{placeholders['slope']}°"
    )
