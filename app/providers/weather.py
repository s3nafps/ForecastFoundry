def request_headers(provider: str) -> dict[str, str]:
    user_agent = "ForecastFoundry/0.1 (provider contact required)"
    if provider in {"nws", "met_no", "aviation_weather"}:
        return {"User-Agent": user_agent, "Accept": "application/json"}
    return {"Accept": "application/json"}
