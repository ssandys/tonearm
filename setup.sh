#!/bin/bash
set -euo pipefail

readonly plugin_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
readonly installed_root="$HOME/.config/omarchy/plugins/ssandys.tonearm"
readonly source_unit="$plugin_root/systemd/tonearmd.service"
readonly unit_dir="$HOME/.config/systemd/user"
readonly target_unit="$unit_dir/tonearmd.service"

missing=()
for command in systemctl curl; do
  command -v "$command" >/dev/null 2>&1 || missing+=("$command")
done
# Probe /usr/bin/python explicitly, not whichever python3 a version manager puts
# first on PATH -- the unit runs under the system interpreter.
/usr/bin/python -c 'import dbus_next' >/dev/null 2>&1 || missing+=("python-dbus-next")
/usr/bin/python -c 'import websocket' >/dev/null 2>&1 || missing+=("python-websocket-client")

if ((${#missing[@]} > 0)); then
  printf 'Missing dependencies: %s\n' "${missing[*]}" >&2
  printf 'Install with: omarchy pkg add %s\n' "${missing[*]}" >&2
  exit 1
fi

if [[ ${1:-} == --check ]]; then
  systemctl --user is-active --quiet tonearmd.service \
    || { echo 'tonearmd.service is not running' >&2; exit 1; }
  "$plugin_root/scripts/tonearmctl" status >/dev/null 2>&1 \
    || { echo 'tonearmd is not answering on its socket' >&2; exit 1; }
  if [[ -e "$HOME/.config/tonearm/token" ]]; then
    printf 'Tonearm setup is healthy.\n'
  else
    printf 'Tonearm is running but NOT PAIRED.\n'
    printf 'Enable it in Roon Remote -> Settings -> Extensions.\n'
  fi
  exit 0
fi

if [[ $(realpath -m "$plugin_root") != $(realpath -m "$installed_root") ]]; then
  printf 'Run setup from the installed plugin: %s/setup.sh\n' "$installed_root" >&2
  exit 1
fi

mkdir -p "$unit_dir"
cp "$source_unit" "$target_unit"
systemctl --user daemon-reload
systemctl --user enable --now tonearmd.service

printf 'tonearmd installed and started.\n'
printf 'Now enable "tonearm" in Roon Remote -> Settings -> Extensions.\n'
printf 'Check with: %s/setup.sh --check\n' "$installed_root"
