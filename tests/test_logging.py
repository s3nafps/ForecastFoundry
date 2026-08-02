import json
import logging

from app.logging import JsonFormatter


def test_json_formatter_redacts_secrets() -> None:
    formatter = JsonFormatter(secrets=("super-secret",))
    record = logging.LogRecord(
        "weatheredge",
        logging.ERROR,
        __file__,
        12,
        "provider failed with token super-secret",
        (),
        None,
    )

    payload = json.loads(formatter.format(record))

    assert payload["message"] == "provider failed with token [REDACTED]"
    assert "super-secret" not in formatter.format(record)
