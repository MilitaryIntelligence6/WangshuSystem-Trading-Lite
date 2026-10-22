#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    python3.13 -m venv .venv
fi
if [ ! -f .venv/lite-installed-v1 ]; then
    .venv/bin/python -m pip install -e '.[desktop,ctp,strategy]'
    touch .venv/lite-installed-v1
fi
exec .venv/bin/python -m wangshu_lite
