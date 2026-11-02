#!/bin/bash
set -euo pipefail

readonly plugin_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
readonly installed_root="$HOME/.config/omarchy/plugins/ssandys.tonearm"
readonly source_unit="$plugin_root/systemd/tonearmd.service"
readonly unit_dir="$HOME/.config/systemd/user"
readonly target_unit="$unit_dir/tonearmd.service"
# The filtered session-bus socket tonearmd publishes MPRIS through. See the
# header of systemd/tonearmd-bus.service for why the daemon does not get the
# session bus itself.
readonly source_bus_unit="$plugin_root/systemd/tonearmd-bus.service"
readonly target_bus_unit="$unit_dir/tonearmd-bus.service"
# Must match the unit's ExecStart and ConditionFileIsExecutable;
# tests/python/test_setup.py asserts that it does.
readonly proxy_bin=/usr/bin/xdg-dbus-proxy
readonly mpris_name=org.mpris.MediaPlayer2.tonearm

# Ownership tests for an existing file at either unit path. The daemon's is
# matched by its exact ExecStart; the proxy's by the socket only it serves.
owns_daemon_unit() {
  grep -Fxq 'ExecStart=%h/.config/omarchy/plugins/ssandys.tonearm/scripts/tonearmd' -- "$1"
}
owns_bus_unit() {
  grep -Eq '^ExecStart=/usr/bin/xdg-dbus-proxy .*%t/tonearm-bus/bus' -- "$1"
}

usage() {
  printf 'Usage: %s [--core HOST [--http-port PORT] [--tcp-port PORT]] | --check | --uninstall\n' \
    "$0" >&2
}

check_only=false
core_set=false
core_host=""
http_port=9330
tcp_port=9150
http_port_set=false
tcp_port_set=false

if (($# == 1)) && [[ $1 == --uninstall ]]; then
  # Uninstall needs only systemctl, not the daemon's Python dependencies. Check
  # ownership before stopping anything: a same-named but unrelated unit is not
  # ours to disable or remove. Leave pairing/configuration state in place.
  if [[ $(realpath -m "$plugin_root") != $(realpath -m "$installed_root") ]]; then
    printf 'Run setup from the installed plugin: %s/setup.sh\n' "$installed_root" >&2
    exit 1
  fi
  if [[ -L "$unit_dir" || -L "$target_unit" || -L "$target_bus_unit" ]]; then
    printf 'Refusing to uninstall: service directory or unit is a symlink.\n' >&2
    exit 1
  fi
  # Both units are vetted before either is stopped or removed.
  for unit in "$target_unit" "$target_bus_unit"; do
    if [[ -e "$unit" && ! -f "$unit" ]]; then
      printf 'Refusing to uninstall: %s is not a regular file.\n' "$unit" >&2
      exit 1
    fi
  done
  if [[ -e "$target_unit" ]] && ! owns_daemon_unit "$target_unit"; then
    printf 'Refusing to uninstall an unrelated service file: %s\n' "$target_unit" >&2
    exit 1
  fi
  if [[ -e "$target_bus_unit" ]] && ! owns_bus_unit "$target_bus_unit"; then
    printf 'Refusing to uninstall an unrelated service file: %s\n' "$target_bus_unit" >&2
    exit 1
  fi
  if [[ -e "$target_unit" || -e "$target_bus_unit" ]]; then
    command -v systemctl >/dev/null 2>&1 || { echo 'systemctl is required' >&2; exit 1; }
    if [[ -e "$target_unit" ]]; then
      systemctl --user disable --now tonearmd.service
      rm -- "$target_unit"
    fi
    # Not enabled on its own (no [Install]); stopping is all there is to undo.
    if [[ -e "$target_bus_unit" ]]; then
      systemctl --user stop tonearmd-bus.service
      rm -- "$target_bus_unit"
    fi
    systemctl --user daemon-reload
  fi
  printf 'tonearmd removed; pairing and configuration were preserved.\n'
  exit 0
elif (($# == 1)) && [[ $1 == --check ]]; then
  check_only=true
  shift
else
  while (($# > 0)); do
    case "$1" in
      --uninstall)
        usage
        exit 2
        ;;
      --core)
        if $core_set || (($# < 2)); then usage; exit 2; fi
        core_host=$2
        core_set=true
        shift 2
        ;;
      --http-port)
        if $http_port_set || (($# < 2)); then usage; exit 2; fi
        http_port=$2
        http_port_set=true
        shift 2
        ;;
      --tcp-port)
        if $tcp_port_set || (($# < 2)); then usage; exit 2; fi
        tcp_port=$2
        tcp_port_set=true
        shift 2
        ;;
      *)
        usage
        exit 2
        ;;
    esac
  done
  if ! $core_set && { $http_port_set || $tcp_port_set; }; then
    printf '%s\n' '--http-port and --tcp-port require --core.' >&2
    usage
    exit 2
  fi
fi

missing=()
for command in systemctl; do
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

if $check_only; then
  systemctl --user is-active --quiet tonearmd.service \
    || { echo 'tonearmd.service is not running' >&2; exit 1; }
  "$plugin_root/scripts/tonearmctl" status >/dev/null 2>&1 \
    || { echo 'tonearmd is not answering on its socket' >&2; exit 1; }
  # Media keys are not load-bearing, so a problem here is reported, not
  # failed -- but it is never reported as healthy.
  media_ok=false
  if [[ ! -x "$proxy_bin" ]]; then
    media="unavailable -- xdg-dbus-proxy is not installed.
  Install with: omarchy pkg add xdg-dbus-proxy, then re-run setup.sh."
  elif busctl --timeout=5 --user status "$mpris_name" >/dev/null 2>&1; then
    media_ok=true
    media="$mpris_name is registered."
  else
    media="unavailable -- $mpris_name is not registered on the session bus.
  See: journalctl --user -u tonearmd -u tonearmd-bus"
  fi
  if [[ -e "$HOME/.config/tonearm/token" ]]; then
    if $media_ok; then
      printf 'Tonearm setup is healthy.\n'
    else
      printf 'Tonearm is running and paired, but media keys do not work.\n'
    fi
  else
    printf 'Tonearm is running but NOT PAIRED.\n'
    printf 'Enable it in Roon Remote -> Settings -> Extensions.\n'
  fi
  printf 'Media keys: %s\n' "$media"
  exit 0
fi

if [[ $(realpath -m "$plugin_root") != $(realpath -m "$installed_root") ]]; then
  printf 'Run setup from the installed plugin: %s/setup.sh\n' "$installed_root" >&2
  exit 1
fi

mkdir -p "$unit_dir"

# Both unit paths are vetted before either is written, so a refusal cannot
# leave a daemon unit that Wants= a proxy unit nobody installed.
for unit in "$target_unit" "$target_bus_unit"; do
  # `cp` FOLLOWS a symlink at its destination, so a link planted at a unit
  # path would redirect this write to whatever it names. Refuse rather than
  # write.
  if [[ -L "$unit" ]]; then
    printf 'Refusing to install: %s is a symlink.\n' "$unit" >&2
    printf 'Remove it and re-run if you did not put it there on purpose.\n' >&2
    exit 1
  fi

  # A socket, FIFO or directory at that path is equally not something to
  # write through.
  if [[ -e "$unit" && ! -f "$unit" ]]; then
    printf 'Refusing to install: %s exists and is not a regular file.\n' "$unit" >&2
    exit 1
  fi
done

# An existing regular file has to be OUR unit. The names are tonearm-specific,
# so an unrelated service sitting there means something unexpected is going on
# and clobbering it would be destructive. The script this was modelled on
# (stappmus.audio) guards the same way.
if [[ -f "$target_unit" ]] && ! grep -q 'tonearmd' -- "$target_unit"; then
  printf 'Refusing to overwrite an unrelated service file: %s\n' "$target_unit" >&2
  exit 1
fi
if [[ -f "$target_bus_unit" ]] && ! owns_bus_unit "$target_bus_unit"; then
  printf 'Refusing to overwrite an unrelated service file: %s\n' "$target_bus_unit" >&2
  exit 1
fi

# An explicit first-run Core bypasses unreliable SOOD discovery. Validation and
# persistence stay in config.py so this shell script never hand-writes JSON and
# the same descriptor-relative, atomic, mode-0600 path protects every config
# write. Run it before touching the unit so bad input cannot enable a service.
if $core_set; then
  PYTHONPATH="$plugin_root/scripts" /usr/bin/python -m tonearm_lib.config \
    bootstrap-core "$core_host" "$http_port" "$tcp_port"
fi

# The daemon's state directory (Core address, pairing token, pinned zone).
# Created HERE rather than by the daemon because the unit runs with
# ProtectSystem=strict and ProtectHome=read-only: $HOME is read-only inside
# the sandbox, and systemd refuses to start a unit whose ReadWritePaths names
# a path that does not exist -- failing at step NAMESPACE before the process
# runs at all. chmod as well as mkdir, since mkdir -p leaves an existing
# directory's mode alone and this one holds the token. bootstrap-core creates
# and tightens it first when used; these operations remain idempotent.
mkdir -p "$HOME/.config/tonearm"
chmod 700 "$HOME/.config/tonearm"

# Unpredictable name, created O_EXCL by mktemp, in the destination's own
# directory so the rename is atomic. Nothing can be pre-planted at a name
# nobody can guess, and systemd never observes a half-written unit.
tmp_unit=""
trap '[[ -z "$tmp_unit" ]] || rm -f -- "$tmp_unit"' EXIT
install_unit() {
  local source=$1 target=$2
  tmp_unit=$(mktemp -- "$unit_dir/.${target##*/}.XXXXXXXX")
  cat -- "$source" > "$tmp_unit"
  chmod 0644 -- "$tmp_unit"
  mv -f -- "$tmp_unit" "$target"
  tmp_unit=""
}
# The proxy first, so the daemon's Wants= never names a unit that is not there.
install_unit "$source_bus_unit" "$target_bus_unit"
install_unit "$source_unit" "$target_unit"
trap - EXIT

# restart, not `enable --now`: --now starts a stopped unit but leaves a running
# one alone, so re-running setup.sh after an update never applied a changed
# unit. restart also starts a stopped one. The proxy needs no call of its own:
# tonearmd Wants= it, and it is PartOf= tonearmd, so it follows the restart.
systemctl --user daemon-reload
systemctl --user enable tonearmd.service
systemctl --user restart tonearmd.service

if [[ ! -x "$proxy_bin" ]]; then
  printf 'Note: xdg-dbus-proxy is not installed, so media keys will be unavailable.\n' >&2
  printf 'Install with: omarchy pkg add xdg-dbus-proxy, then re-run setup.sh.\n' >&2
fi

printf 'tonearmd installed and (re)started.\n'
printf 'Now enable "tonearm" in Roon Remote -> Settings -> Extensions.\n'
printf 'Check with: %s/setup.sh --check\n' "$installed_root"
