#!/usr/bin/env bash
set -euo pipefail

journalctl -u gpt-market-tunnel.service --no-pager -n 500 \
  | grep -Eo 'https://[-a-z0-9]+\.trycloudflare\.com' \
  | tail -n 1
