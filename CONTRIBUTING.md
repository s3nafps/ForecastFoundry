# Contributing

Contributions are welcome under Apache-2.0. Please include focused tests for
parsers, money/security paths, and any non-trivial model logic. Keep paper mode
as the default, do not commit secrets or private keys, and preserve migration
compatibility with the existing `weatheredge.db` tables. Run `pytest -q` and
`ruff check .` before submitting a pull request.
