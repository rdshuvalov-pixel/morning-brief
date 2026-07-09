#!/bin/bash
# /root/morning_brief_v2/scripts/manual/generate_llm.sh
# Wrapper for generate_llm.py — so worker can call it as a bash script uniformly.
# Usage: generate_llm.sh --date YYYY-MM-DD [--write]
set -eo pipefail
cd /root/morning_brief_v2
exec ./.venv/bin/python scripts/manual/generate_llm.py "$@"
