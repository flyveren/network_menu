#!/usr/bin/env python3
"""Fetch current weather data for Maribo and persist it as JSON."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup


WEATHER_URL = "https://vejr.tv2.dk/vejr/maribo-2617072"
USER_AGENT = "navigation-demo/1.0 (+https://vejr.tv2.dk/vejr/maribo-2617072)"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "weather.json"


def fetch_html() -> bytes:
    request = Request(
        WEATHER_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "da,en;q=0.9",
        },
    )
    with urlopen(request, timeout=15) as response:
        return response.read()


def parse_weather(html: bytes) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    widget = soup.select_one(".tc_weather__forecast__widget")
    if widget is None:
        raise ValueError("Unable to locate weather widget in HTML document")

    def clean_temperature(text: str | None) -> tuple[float | None, str | None]:
        if not text:
            return None, None
        value_text = text.strip()
        match = re.search(r"-?\d+(?:[.,]\d+)?", value_text.replace("°", ""))
        value = float(match.group().replace(",", ".")) if match else None
        return value, value_text

    temperature_node = widget.select_one(".tc_weather__forecast__widget__temperature")
    feels_like_node = widget.select_one(".tc_weather__forecast__widget__windchill")

    temperature_value, temperature_text = clean_temperature(
        temperature_node.get_text(strip=True) if temperature_node else None
    )
    feels_like_value, feels_like_text = clean_temperature(
        feels_like_node.get_text(strip=True) if feels_like_node else None
    )

    icon_node = widget.select_one(".tc_weather__forecast__widget__symbol")
    icon_url = icon_node["src"] if icon_node and icon_node.has_attr("src") else None
    icon_description = icon_node.get("alt") if icon_node else None

    info_items = widget.select(".tc_weather__forecast__widget__info li")
    precipitation_mm: float | None = None
    precipitation_text: str | None = None
    wind_speed: float | None = None
    wind_text: str | None = None
    wind_direction: str | None = None

    if info_items:
        if len(info_items) >= 1:
            rain_strong = info_items[0].select_one("strong")
            if rain_strong:
                precipitation_text = rain_strong.get_text(strip=True)
                try:
                    precipitation_mm = float(
                        precipitation_text.replace(",", ".").replace("-", "0")
                    )
                except ValueError:
                    precipitation_mm = None
        if len(info_items) >= 2:
            wind_strong = info_items[1].select_one("strong")
            if wind_strong:
                wind_text = wind_strong.get_text(strip=True)
                try:
                    wind_speed = float(
                        wind_text.replace(",", ".").replace("-", "0")
                    )
                except ValueError:
                    wind_speed = None
            wind_direction = info_items[1].get("title")

    return {
        "source": WEATHER_URL,
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "temperature_c": temperature_value,
        "temperature_text": temperature_text,
        "feels_like_c": feels_like_value,
        "feels_like_text": feels_like_text,
        "symbol_url": icon_url,
        "symbol_description": icon_description,
        "precipitation_mm": precipitation_mm,
        "precipitation_text": precipitation_text,
        "wind_speed_ms": wind_speed,
        "wind_text": wind_text,
        "wind_direction": wind_direction,
    }


def write_payload(payload: dict[str, Any]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = OUTPUT_PATH.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(OUTPUT_PATH)


def main() -> int:
    try:
        html = fetch_html()
        payload = parse_weather(html)
        write_payload(payload)
    except (HTTPError, URLError, TimeoutError) as exc:
        print(f"[fetch_weather] Failed to fetch weather: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[fetch_weather] Unexpected error: {exc}", file=sys.stderr)
        return 1

    print(f"[fetch_weather] Weather data updated at {payload['fetchedAt']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

