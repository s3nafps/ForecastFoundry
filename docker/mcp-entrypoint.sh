#!/bin/sh
set -eu

if [ "${EXECUTION_ENABLED:-false}" != "false" ]; then
  echo "MCP refuses to start with execution enabled" >&2
  exit 1
fi
exec forecastfoundry mcp
