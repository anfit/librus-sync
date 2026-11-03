#!/usr/bin/env bash
# One scheduled run (systemd oneshot). Builds a rollback-safe venv in persistent
# storage, then emails new/changed/removed calendar events.
set -euo pipefail

umask 077
app_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$app_dir"
runtime_dir="${LIBRUS_RUNTIME_DIR:-/var/lib/librus-sync}"
state="${LIBRUS_STATE_PATH:-$runtime_dir/state.json}"
config="${LIBRUS_CONFIG_PATH:-config/production.yaml}"
required="$(sha256sum requirements.txt | cut -d' ' -f1)"
venv="$runtime_dir/venv-$required"

export HOME="$runtime_dir"
export PYTHONDONTWRITEBYTECODE=1

mkdir -p "$runtime_dir"
if [[ ! -x "$venv/bin/python" ]]; then
  rm -rf "$venv"
  python3 -m venv "$venv"
  "$venv/bin/python" -m pip install --disable-pip-version-check -r requirements.txt
fi

mode=--send
if [[ ! -f "$state" ]]; then
  # A fresh deployment must never mass-mail events the family already received:
  # record what is currently visible and only notify about changes from now on.
  echo "No state at $state: recording a baseline instead of sending."
  mode=--baseline
fi

exec "$venv/bin/python" sync.py --config "$config" --state "$state" "$mode"
