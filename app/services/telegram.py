from collections.abc import Mapping

from app.schemas import PaperAlert
from app.services.http import ProviderResponseError, ResilientHttpClient


def format_signal_alert(alert: PaperAlert) -> str:
    models = "\n".join(
        f"{model}: {counts[0]}/{counts[1]} members"
        for model, counts in sorted(alert.model_member_counts.items())
    )
    return f"""🌦 FORECASTFOUNDRY PAPER SIGNAL

Market:
{alert.question}

Outcome:
{alert.outcome}

Model probability: {alert.model_probability:.1%}
Executable best ask: {alert.executable_ask:.1%}
Raw edge: {alert.raw_edge:.1%}
Usable edge: {alert.usable_edge:.1%}

Models:
{models}

Resolution station: {alert.station_id}
Observation so far: {alert.observation_summary}
Forecast horizon: {alert.forecast_horizon_hours} hours
Spread: {alert.spread:.0%}
Rule confidence: {alert.rule_confidence}/100

Mode: PAPER ONLY"""


class TelegramClient:
    def __init__(self, http: ResilientHttpClient, *, token: str, chat_id: int) -> None:
        self._http = http
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id

    async def send_signal(self, alert: PaperAlert) -> bool:
        payload = await self._http.request_json(
            "POST",
            self._url,
            json={
                "chat_id": self._chat_id,
                "text": format_signal_alert(alert),
                "disable_web_page_preview": True,
            },
        )
        if not isinstance(payload, Mapping) or payload.get("ok") is not True:
            raise ProviderResponseError("Telegram rejected the alert")
        return True
