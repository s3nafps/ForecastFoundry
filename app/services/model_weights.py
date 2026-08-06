from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApplicationSetting

WEIGHTS_KEY = "model_weights:weather"


@dataclass(frozen=True)
class ModelSample:
    model: str
    probability: Decimal
    won: bool


def extract_samples(
    signals: Iterable[tuple[Mapping[str, object], bool]],
) -> dict[str, list[ModelSample]]:
    """Build per-model samples from (signal_data, won) pairs."""
    samples: dict[str, list[ModelSample]] = {}
    for signal_data, won in signals:
        raw = signal_data.get("model_probabilities")
        if not isinstance(raw, Mapping):
            continue
        for model, probability in raw.items():
            if not isinstance(model, str):
                continue
            try:
                value = Decimal(str(probability))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if 0 <= value <= 1:
                samples.setdefault(model, []).append(ModelSample(model, value, won))
    return samples


def model_brier(samples: Mapping[str, list[ModelSample]]) -> dict[str, Decimal]:
    return {
        model: sum(
            (sample.probability - Decimal(int(sample.won))) ** 2 for sample in entries
        ) / Decimal(len(entries))
        for model, entries in samples.items()
    }


def compute_weights(
    samples: Mapping[str, list[ModelSample]],
    *,
    min_samples: int = 30,
    min_improvement: Decimal = Decimal("0.05"),
) -> dict[str, float] | None:
    if not samples or any(len(entries) < min_samples for entries in samples.values()):
        return None
    brier = model_brier(samples)
    perfect = next((model for model, score in brier.items() if score == 0), None)
    if perfect is not None:
        # A Brier of exactly 0 means the model is perfectly calibrated; give it all
        # the weight so the blend degenerates to that model (weights still sum to 1).
        return {model: 1.0 if model == perfect else 0.0 for model in brier}
    baseline = sum(brier.values()) / Decimal(len(brier))
    inverse = {model: Decimal("1") / score for model, score in brier.items()}
    total = sum(inverse.values())
    weights = {model: float(weight / total) for model, weight in inverse.items()}
    blend_brier = sum(
        brier[model] * Decimal(str(weights[model])) for model in brier
    )
    if baseline - blend_brier < min_improvement * baseline:
        return None
    return weights


async def load_model_weights(session: AsyncSession) -> dict[str, float]:
    setting = await session.get(ApplicationSetting, WEIGHTS_KEY)
    if setting is None or not isinstance(setting.value, Mapping):
        return {}
    weights: dict[str, float] = {}
    for model, weight in setting.value.items():
        try:
            weights[str(model)] = float(weight)
        except (TypeError, ValueError):
            continue
    return weights


async def store_model_weights(
    session: AsyncSession, weights: Mapping[str, float]
) -> None:
    setting = await session.get(ApplicationSetting, WEIGHTS_KEY)
    if setting is None:
        setting = ApplicationSetting(key=WEIGHTS_KEY, value={})
        session.add(setting)
    setting.value = {str(model): float(weight) for model, weight in weights.items()}
