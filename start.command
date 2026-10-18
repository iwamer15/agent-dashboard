#!/bin/bash
# Agent Dashboard 起動 (mac) — ダブルクリックで起動し、ブラウザが開きます
cd "$(dirname "$0")"
python3 -m agentdash "$@"
