from __future__ import annotations

import requests

from core.config import Settings
from core.schemas import GeoMetadata


class ReverseGeocoder:
    def __init__(self, settings: Settings):
        self.settings = settings

    def reverse(self, lat: float | None, lon: float | None) -> GeoMetadata:
        if not self.settings.geocode_enabled or lat is None or lon is None:
            return GeoMetadata()

        try:
            response = requests.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={
                    "format": "jsonv2",
                    "lat": lat,
                    "lon": lon,
                },
                headers={"User-Agent": self.settings.geocode_user_agent},
                timeout=10,
            )
            response.raise_for_status()
        except requests.RequestException:
            return GeoMetadata()

        payload = response.json()
        address = payload.get("address") if isinstance(payload, dict) else {}
        address = address if isinstance(address, dict) else {}

        return GeoMetadata(
            place_name=payload.get("display_name") if isinstance(payload, dict) else None,
            country=address.get("country"),
        )
