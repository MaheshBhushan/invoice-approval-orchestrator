#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
python scripts/recovery_demo.py
