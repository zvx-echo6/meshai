"""Weather command handler."""

import logging
from typing import Optional

import httpx

from .base import CommandContext, CommandHandler

logger = logging.getLogger(__name__)


class WeatherCommand(CommandHandler):
    """Get weather information."""

    name = "weather"
    description = "Get weather info"
    usage = "!weather [location]"

    async def execute(self, args: str, context: CommandContext) -> str:
        """Get weather for location or sender's GPS position."""
        config = context.config.weather

        # Determine location
        location = await self._resolve_location(args.strip(), context)

        if location is None:
            return "No location available. Use !weather <city> or enable GPS on your node."

        # Try primary provider
        result = await self._fetch_weather(config.primary, location, context)

        if result is None and config.fallback and config.fallback != "none":
            # Try fallback
            logger.debug(f"Primary weather provider failed, trying fallback: {config.fallback}")
            result = await self._fetch_weather(config.fallback, location, context)

        if result is None:
            return "Weather lookup failed. Try again later."

        return result

    async def _resolve_location(
        self, args: str, context: CommandContext
    ) -> Optional[str | tuple[float, float]]:
        """Resolve location from args, GPS, or config default.

        Returns:
            Location string, (lat, lon) tuple, or None
        """
        # 1. If location provided in args, use it
        if args:
            return args

        # 2. Try sender's GPS position
        if context.position:
            return context.position

        # 3. Fall back to config default
        default = context.config.weather.default_location
        if default:
            return default

        return None

    async def _fetch_weather(
        self,
        provider: str,
        location: str | tuple[float, float],
        context: CommandContext,
    ) -> Optional[str]:
        """Fetch weather from specified provider."""
        try:
            if provider == "openmeteo":
                return await self._fetch_openmeteo(location, context)
            elif provider == "wttr":
                return await self._fetch_wttr(location, context)
            elif provider == "llm":
                return await self._fetch_llm(location, context)
            else:
                logger.warning(f"Unknown weather provider: {provider}")
                return None
        except Exception as e:
            logger.error(f"Weather fetch error ({provider}): {e}")
            return None

    async def _fetch_openmeteo(
        self,
        location: str | tuple[float, float],
        context: CommandContext,
    ) -> Optional[str]:
        """Fetch weather from Open-Meteo API."""
        base_url = context.config.weather.openmeteo.url

        # Get coordinates
        if isinstance(location, tuple):
            lat, lon = location
        else:
            # Geocode the location name
            coords = await self._geocode(location)
            if coords is None:
                return None
            lat, lon = coords

        # Fetch current weather + 3-day forecast
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{base_url}/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,weathercode,windspeed_10m",
                    "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "temperature_unit": "fahrenheit",
                    "windspeed_unit": "mph",
                    "forecast_days": 3,
                    "timezone": "auto",
                },
            )
            response.raise_for_status()
            data = response.json()

        current = data.get("current", {})
        temp = current.get("temperature_2m")
        code = current.get("weathercode", 0)
        wind = current.get("windspeed_10m")

        if temp is None:
            return None

        # Convert weather code to description
        condition = self._weather_code_to_text(code)

        # Format location name
        loc_name = location if isinstance(location, str) else f"{lat:.2f},{lon:.2f}"

        # Build current conditions
        result = f"{loc_name}: {temp:.0f}F, {condition}, Wind {wind:.0f}mph"

        # Add forecast
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])
        lows = daily.get("temperature_2m_min", [])
        codes = daily.get("weathercode", [])
        precip = daily.get("precipitation_probability_max", [])

        if dates and len(dates) >= 3:
            # Skip today (index 0), show next 2 days
            forecast_parts = []
            day_names = ["Today", "Tomorrow"]
            for i in range(1, min(3, len(dates))):
                day = day_names[i-1] if i <= len(day_names) else dates[i]
                hi = highs[i] if i < len(highs) else None
                lo = lows[i] if i < len(lows) else None
                cond = self._weather_code_to_text(codes[i]) if i < len(codes) else ""
                rain = precip[i] if i < len(precip) else 0

                if hi is not None and lo is not None:
                    part = f"{day}: {lo:.0f}-{hi:.0f}F {cond}"
                    if rain and rain > 20:
                        part += f" {rain}%rain"
                    forecast_parts.append(part)

            if forecast_parts:
                result += " | " + ", ".join(forecast_parts)

        return result

    async def _fetch_wttr(
        self,
        location: str | tuple[float, float],
        context: CommandContext,
    ) -> Optional[str]:
        """Fetch weather from wttr.in."""
        base_url = context.config.weather.wttr.url

        # Format location for wttr.in
        if isinstance(location, tuple):
            lat, lon = location
            loc_param = f"{lat},{lon}"
        else:
            loc_param = location.replace(" ", "+")

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{base_url}/{loc_param}",
                params={"format": "%l:+%t,+%C,+Wind+%w"},
                headers={"User-Agent": "MeshAI/1.0"},
            )
            response.raise_for_status()

        return response.text.strip()

    async def _fetch_llm(
        self,
        location: str | tuple[float, float],
        context: CommandContext,
    ) -> Optional[str]:
        """Let LLM fetch weather via web search.

        This is a placeholder - actual implementation would route
        to the LLM backend with a weather query.
        """
        # For now, return None to indicate this provider isn't fully implemented
        # The router will handle LLM queries separately
        logger.debug("LLM weather provider not yet integrated")
        return None

    async def _geocode(self, location: str) -> Optional[tuple[float, float]]:
        """Geocode a location name to coordinates using Open-Meteo geocoding."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location, "count": 1},
            )
            response.raise_for_status()
            data = response.json()

        results = data.get("results", [])
        if not results:
            return None

        return (results[0]["latitude"], results[0]["longitude"])

    def _weather_code_to_text(self, code: int) -> str:
        """Convert WMO weather code to text description."""
        codes = {
            0: "Clear",
            1: "Mostly Clear",
            2: "Partly Cloudy",
            3: "Cloudy",
            45: "Foggy",
            48: "Fog",
            51: "Light Drizzle",
            53: "Drizzle",
            55: "Heavy Drizzle",
            61: "Light Rain",
            63: "Rain",
            65: "Heavy Rain",
            71: "Light Snow",
            73: "Snow",
            75: "Heavy Snow",
            77: "Snow Grains",
            80: "Light Showers",
            81: "Showers",
            82: "Heavy Showers",
            85: "Light Snow Showers",
            86: "Snow Showers",
            95: "Thunderstorm",
            96: "Thunderstorm w/ Hail",
            99: "Severe Thunderstorm",
        }
        return codes.get(code, "Unknown")
