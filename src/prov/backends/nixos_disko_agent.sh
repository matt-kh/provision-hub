#!/usr/bin/env bash
# Rendered by prov (os-install, backend nixos-disko) for [state.report]; edit spec.toml and re-render.
# prov-state-agent: report-only state agent (docs/state.md). It writes this host's state
# record, <kind>/<name>.json, to the state repository and never changes the system. It
# reads its settings from the environment (prov-state.nix sets them):
#   PROV_STATE_REF          <kind>/<name> of this host's item
#   PROV_STATE_REPO         ssh git URL of the state repository
#   PROV_STATE_BRANCH       branch to write to
#   PROV_STATE_HEARTBEAT    push at least this often (<n>s|m|h|d) when nothing else changed
#   PROV_STATE_IDENTITY     path of the deploy key (only ssh reads it; never printed)
#   PROV_STATE_KNOWN_HOSTS  file with the repository host's public keys (host keys are checked)
#   PROV_STATE_DIR          working directory (default /var/lib/prov-state)
#   PROV_STATE_ROOT         prefix for /run, /nix and /proc (tests only; default empty)
set -euo pipefail

AGENT_VERSION="prov-state-agent/1"
API_VERSION="prov.state/v1alpha1"

die() {
  echo "prov-state-agent: $*" >&2
  exit 1
}

for var in PROV_STATE_REF PROV_STATE_REPO PROV_STATE_BRANCH PROV_STATE_HEARTBEAT PROV_STATE_IDENTITY PROV_STATE_KNOWN_HOSTS; do
  if [ -z "${!var:-}" ]; then
    die "$var is not set"
  fi
done

root="${PROV_STATE_ROOT:-}"
dir="${PROV_STATE_DIR:-/var/lib/prov-state}"
ref="$PROV_STATE_REF"
branch="$PROV_STATE_BRANCH"
kind="${ref%%/*}"
name="${ref#*/}"
case "$ref" in
  */*/* | /* | */ | *..*) die "PROV_STATE_REF must be <kind>/<name>" ;;
  */*) ;;
  *) die "PROV_STATE_REF must be <kind>/<name>" ;;
esac

# Settings reach GIT_SSH_COMMAND, which git runs through a shell: re-check their shape.
for path in "$PROV_STATE_IDENTITY" "$PROV_STATE_KNOWN_HOSTS" "$dir"; do
  if ! [[ "$path" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    die "unsafe path $path (absolute, letters, digits and ._/- only)"
  fi
done

if [ ! -s "$PROV_STATE_KNOWN_HOSTS" ]; then
  die "known_hosts file $PROV_STATE_KNOWN_HOSTS is missing or empty; refusing to connect without a pinned host key"
fi
if [ ! -f "$PROV_STATE_IDENTITY" ]; then
  die "deploy key $PROV_STATE_IDENTITY is missing; place the state repository's deploy key there (docs/state.md)"
fi

seconds() {
  local n="${1%[smhd]}"
  if ! [[ "$n" =~ ^[1-9][0-9]{0,5}$ ]]; then
    die "invalid duration $1 (expected <n>s|m|h|d)"
  fi
  case "${1: -1}" in
    s) echo "$n" ;;
    m) echo $((n * 60)) ;;
    h) echo $((n * 3600)) ;;
    d) echo $((n * 86400)) ;;
    *) die "invalid duration $1 (expected <n>s|m|h|d)" ;;
  esac
}
heartbeat=$(seconds "$PROV_STATE_HEARTBEAT")

# --- observe ----------------------------------------------------------------------------

resolve() {
  readlink -f "$1" 2>/dev/null || true
}
first_line() {
  if [ -r "$1" ]; then
    head -n 1 "$1" || true
  fi
}

profiles="$root/nix/var/nix/profiles"
current=$(resolve "$root/run/current-system")
booted=$(resolve "$root/run/booted-system")
profile=$(resolve "$profiles/system")
staged=""
generation=""
link=$(readlink "$profiles/system" 2>/dev/null || true)
if [[ "$link" =~ ^system-([0-9]+)-link$ ]]; then
  generation="${BASH_REMATCH[1]}"
fi
newest=""
for entry in "$profiles"/system-*-link; do
  base="${entry##*/}"
  if [[ "$base" =~ ^system-([0-9]+)-link$ ]]; then
    n="${BASH_REMATCH[1]}"
    if [ -z "$newest" ] || [ "$n" -gt "$newest" ]; then
      newest="$n"
    fi
  fi
done

digest=""
if [ -n "$current" ]; then
  digest=$(first_line "$current/etc/prov/render-digest")
fi
if ! [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  digest=""
fi

status="clean"
detail=""
if [ -z "$current" ] || [ -z "$profile" ]; then
  status="unknown"
  detail="cannot resolve /run/current-system or the system profile"
elif [ "$current" != "$profile" ] && [ "$current" = "$booted" ] && [ -n "$generation" ] && [ "$generation" = "$newest" ]; then
  # nixos-rebuild boot: the newest generation waits for the next boot while the booted
  # system runs. Not drift; reported as staged_generation and a pending reboot.
  staged="$generation"
  generation=""
elif [ "$current" != "$profile" ]; then
  status="drifted"
  detail="the running system is not the system profile (nixos-rebuild test or a manual activation)"
elif [ -n "$generation" ] && [ -n "$newest" ] && [ "$generation" -lt "$newest" ]; then
  status="drifted"
  detail="rolled back: generation $generation is active, generation $newest is the newest"
fi
if [ "$status" = "clean" ] && [ -z "$digest" ]; then
  status="unknown"
  detail="the running system carries no render digest (built without [state.report])"
fi

if [ -n "$staged" ]; then
  for entry in "$profiles"/system-*-link; do
    base="${entry##*/}"
    if [[ "$base" =~ ^system-([0-9]+)-link$ ]] && [ "$(resolve "$entry")" = "$current" ]; then
      generation="${BASH_REMATCH[1]}"
    fi
  done
fi

reboot_pending="false"
if [ -n "$staged" ]; then
  reboot_pending="true"
fi
if [ -n "$booted" ] && [ -n "$current" ]; then
  for part in kernel initrd kernel-modules; do
    if [ "$(resolve "$booted/$part")" != "$(resolve "$current/$part")" ]; then
      reboot_pending="true"
    fi
  done
fi

addresses="[]"
if command -v ip >/dev/null 2>&1; then
  if out=$(ip -j addr show scope global 2>/dev/null) &&
    parsed=$(jq -c '[.[]? | .addr_info[]? | select(.local != null) | "\(.local)/\(.prefixlen)"] | .[:64]' <<<"$out" 2>/dev/null) &&
    jq -e 'type == "array"' <<<"$parsed" >/dev/null 2>&1; then
    addresses="$parsed"
  fi
fi

nixos_version=""
if [ -n "$current" ]; then
  nixos_version=$(first_line "$current/nixos-version")
fi

now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
now_s=$(date -u -d "$now" +%s)

# Every string single-line printable ASCII: anything else becomes "?" (docs/state.md).
record=$(
  jq -n -S \
    --arg api "$API_VERSION" \
    --arg ref "$ref" \
    --arg now "$now" \
    --arg status "$status" \
    --arg detail "$detail" \
    --arg digest "$digest" \
    --arg generation "$generation" \
    --arg agent "$AGENT_VERSION" \
    --arg hostname "$(first_line "$root/proc/sys/kernel/hostname")" \
    --arg kernel "$(first_line "$root/proc/sys/kernel/osrelease")" \
    --arg nixos "$nixos_version" \
    --argjson reboot "$reboot_pending" \
    --arg staged "$staged" \
    --argjson addresses "$addresses" \
    '
    def clean: walk(if type == "string" then gsub("[^ -~]"; "?") else . end);
    def opt($k; $v): if $v == "" then {} else {($k): $v} end;
    ({api_version: $api, ref: $ref, last_seen: $now, agent_version: $agent,
      drift: ({status: $status} + opt("detail"; $detail)),
      facts: (opt("hostname"; $hostname) + opt("kernel"; $kernel) + opt("nixos_version"; $nixos)
              + opt("staged_generation"; $staged) + {reboot_pending: $reboot, addresses: $addresses})}
     + opt("render_digest"; $digest) + opt("generation"; $generation)) | clean
    '
)

# --- publish ----------------------------------------------------------------------------

export HOME="$dir"
export GIT_CONFIG_NOSYSTEM=1
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -i $PROV_STATE_IDENTITY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$PROV_STATE_KNOWN_HOSTS -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=3"

repo="$dir/repo"
file="$repo/$kind/$name.json"
# core.symlinks=false: another host's commit can never make this checkout write through a
# symlink (the state repository is shared; docs/state.md, trust model).
git_() {
  git -C "$repo" -c core.symlinks=false -c user.name=prov-state-agent -c user.email=prov-state-agent@localhost -c commit.gpgsign=false "$@"
}

# The checkout is disposable (a depth-1 fetch): start from scratch every run, so a crash, a
# stale lock or a corrupted checkout can never stop the agent for good.
mkdir -p "$dir"
rm -rf "$repo"
git init -q "$repo"
git_ remote add origin "$PROV_STATE_REPO"

# Make the checkout equal the remote branch (or an empty orphan branch when it does not
# exist yet). The checkout is the agent's own: local changes in it are discarded.
sync_branch() {
  local heads
  heads=$(git_ ls-remote --heads origin "refs/heads/$branch") || die "cannot reach the state repository"
  if [ -n "$heads" ]; then
    git_ fetch -q --depth 1 origin "refs/heads/$branch" || die "cannot fetch $branch from the state repository"
    git_ checkout -q -f -B "$branch" FETCH_HEAD
    git_ reset -q --hard FETCH_HEAD
  else
    # The branch does not exist yet: start it unborn, with an empty index.
    git_ symbolic-ref HEAD "refs/heads/$branch"
    git_ update-ref -d "refs/heads/$branch" 2>/dev/null || true
    git_ rm -rq --cached --ignore-unmatch . >/dev/null
  fi
  git_ clean -qfdx
}

# A record path that is a symlink, or a <kind> that is not a directory, is never read or
# written through (with core.symlinks=false a committed symlink arrives as a plain file).
check_paths() {
  if [ -L "$repo/$kind" ] || [ -L "$file" ] || { [ -e "$repo/$kind" ] && [ ! -d "$repo/$kind" ]; } ||
    { [ -e "$file" ] && [ ! -f "$file" ]; }; then
    die "$kind/$name.json in the state repository is a symlink or not a regular file; refusing to touch it"
  fi
}

sync_branch
check_paths
if [ -f "$file" ]; then
  previous=$(cat "$file")
  if same=$(jq -n --argjson a "$previous" --argjson b "$record" '($a | del(.last_seen)) == ($b | del(.last_seen))' 2>/dev/null) && [ "$same" = "true" ]; then
    seen=$(jq -r '.last_seen // empty' <<<"$previous")
    if seen_s=$(date -u -d "$seen" +%s 2>/dev/null) && [ $((now_s - seen_s)) -lt "$heartbeat" ] && [ $((now_s - seen_s)) -ge 0 ]; then
      echo "prov-state-agent: $ref $status, unchanged; last pushed at $seen"
      exit 0
    fi
  fi
fi

for attempt in 1 2 3; do
  check_paths
  mkdir -p "$repo/$kind"
  printf '%s\n' "$record" >"$file.tmp"
  mv "$file.tmp" "$file"
  git_ add -- "$kind/$name.json"
  if ! git_ diff --cached --quiet; then
    git_ commit -q -m "state: $ref $status"
  fi
  if git_ push -q origin "HEAD:refs/heads/$branch"; then
    echo "prov-state-agent: $ref $status, pushed"
    exit 0
  fi
  echo "prov-state-agent: push rejected (attempt $attempt), retrying on the current branch" >&2
  sync_branch
done
die "could not push the record after 3 attempts"
