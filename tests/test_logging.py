import json
import logging

from app.logging import JsonFormatter, configure_logging


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


def test_configure_logging_installs_json_redaction() -> None:
    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers, root.level
    uvicorn_loggers = [
        logging.getLogger(name) for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    ]
    uvicorn_states = [(logger.handlers, logger.propagate) for logger in uvicorn_loggers]
    try:
        configure_logging("WARNING", secrets=("super-secret",))

        assert root.level == logging.WARNING
        assert len(root.handlers) == 1
        formatter = root.handlers[0].formatter
        assert isinstance(formatter, JsonFormatter)
        record = logging.LogRecord(
            "weatheredge", logging.ERROR, __file__, 1, "super-secret", (), None
        )
        assert "super-secret" not in formatter.format(record)
    finally:
        root.handlers, root.level = previous_handlers, previous_level
        for logger, (handlers, propagate) in zip(uvicorn_loggers, uvicorn_states, strict=True):
            logger.handlers, logger.propagate = handlers, propagate
