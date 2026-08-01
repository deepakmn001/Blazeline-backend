import requests
from django.conf import settings


class LocationService:

    URL = "https://api.geoapify.com/v1/geocode/reverse"

    @classmethod
    def reverse_geocode(cls, latitude: float, longitude: float):

        if not settings.GEOAPIFY_API_KEY:
            raise RuntimeError("GEOAPIFY_API_KEY is not configured.")

        try:
            response = requests.get(
                cls.URL,
                params={
                    "lat": latitude,
                    "lon": longitude,
                    "format": "json",
                    "apiKey": settings.GEOAPIFY_API_KEY,
                    "filter": f"circle:{longitude},{latitude},1000",
                },
                timeout=8,
            )

            response.raise_for_status()

            print(response.status_code)
            print(response.text)

        except requests.RequestException:
            return None

        data = response.json()

        results = data.get("results", [])

        if not results:
            return None

        result = results[0]

        postcode = (
            str(result.get("postcode", "")).strip()
            or str(result.get("postal_code", "")).strip()
        )

        if not postcode:
            print("Geoapify Response:", result)
            return None

        return {
            "postcode": postcode,
            "city": (
                result.get("city")
                or result.get("town")
                or result.get("state")
                or ""
            ),
            "area": (
                result.get("suburb")
                or result.get("district")
                or result.get("neighbourhood")
                or result.get("quarter")
                or ""
            ),
        }