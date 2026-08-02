import re


def sanitize_research_text(value: str, *, limit: int = 4000) -> str:
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    return clean[:limit]
