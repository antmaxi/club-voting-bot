#!/bin/bash
#
# Deploy clean main checkouts independently after an activity check.
# --auto leaves stopped bots alone and retries pending deployments.
# --yes confirms idle bots; --skip-active skips ACTIVE/UNKNOWN without prompts.
# --check-only reports status; --threshold MINUTES defaults to 10.
# Main admin activity is recorded separately and excluded from idleness.

set -uo pipefail

# Execute an immutable snapshot, including helpers, before any checkout changes.
if [ "${DEPLOY_SNAPSHOT:-0}" != 1 ]; then
    original_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    snapshot="$(mktemp -d)" || exit 1
    trap 'rm -rf "$snapshot"' EXIT
    cp "$original_dir"/{deploy_bots.sh,load_deploy_repos.sh,check_bot_idle.py} "$snapshot/" || exit 1
    DEPLOY_SNAPSHOT=1 DEPLOY_ENV_FILE="${DEPLOY_ENV_FILE:-$original_dir/../.env}" \
      DEPLOY_LOG_FILE="$original_dir/deploy_bots.log" bash "$snapshot/deploy_bots.sh" "$@"
    exit $?
fi

IDLE_THRESHOLD_MINUTES=10
BOT_UID="$(id -u)"
BOT_GID="$(id -g)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IDLE_CHECKER="${SCRIPT_DIR}/check_bot_idle.py"
LOG_FILE="${DEPLOY_LOG_FILE:-${SCRIPT_DIR}/deploy_bots.log}"

# shellcheck source=load_deploy_repos.sh
source "${SCRIPT_DIR}/load_deploy_repos.sh"

# --- Flags -------------------------------------------------------------
AUTOMATIC=0
AUTO_YES=0        # --yes: don't prompt for IDLE repos
SKIP_ACTIVE=0     # --skip-active: don't prompt for ACTIVE/UNKNOWN repos either, just skip them
CHECK_ONLY=0      # --check-only: report and exit, touch nothing

while [ $# -gt 0 ]; do
    case "$1" in
        --auto) AUTOMATIC=1; AUTO_YES=1; SKIP_ACTIVE=1 ;;
        --yes|-y) AUTO_YES=1 ;;
        --skip-active) SKIP_ACTIVE=1 ;;
        --check-only) CHECK_ONLY=1 ;;
        --threshold) [ $# -ge 2 ] || exit 2; IDLE_THRESHOLD_MINUTES="$2"; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

threshold_display="$(python3 "$IDLE_CHECKER" --format-duration "$IDLE_THRESHOLD_MINUTES")" || exit 2

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG_FILE"
}

compose() {
    local repo="$1"
    shift
    (cd "$repo" && BOT_UID="$BOT_UID" BOT_GID="$BOT_GID" docker compose "$@")
}

confirm() {
    # confirm "prompt" "default(y|n)" -> 0 if the operator said yes
    local prompt="$1" default="$2" reply
    local hint="y/N"
    [ "$default" = "y" ] && hint="Y/n"
    read -r -p "$prompt [$hint] " reply
    reply="${reply:-$default}"
    [[ "$reply" =~ ^[Yy] ]]
}

UPDATED=()
SKIPPED=()
FAILED=()
TO_DEPLOY=()

# After a successful print_repo_status: up-to-date | behind | check-failed
REPO_UPSTREAM_STATUS=""
REPO_UPSTREAM_REF=""

# Fetch origin and compare HEAD to the tracking branch. Sets REPO_UPSTREAM_STATUS and
# REPO_UPSTREAM_LINE (human-readable). Returns 0 if the repo is a valid git checkout.
check_upstream_updates() {
    local repo="$1"
    REPO_UPSTREAM_STATUS="check-failed"
    REPO_UPSTREAM_LINE="(could not check — see log)"
    REPO_UPSTREAM_REF=""

    if ! git -C "$repo" fetch origin --quiet 2>>"$LOG_FILE"; then
        log "[$REPO_NAME] WARN — git fetch origin failed"
        return 0
    fi

    local upstream_ref=""
    if git -C "$repo" rev-parse '@{u}' >/dev/null 2>&1; then
        upstream_ref='@{u}'
    elif git -C "$repo" rev-parse --verify origin/main >/dev/null 2>&1; then
        upstream_ref=origin/main
    elif git -C "$repo" rev-parse --verify origin/master >/dev/null 2>&1; then
        upstream_ref=origin/master
    else
        log "[$REPO_NAME] WARN — no upstream branch (set tracking branch or origin/main)"
        REPO_UPSTREAM_LINE="(no upstream branch)"
        return 0
    fi

    REPO_UPSTREAM_REF="$upstream_ref"

    local behind
    behind="$(git -C "$repo" rev-list --count HEAD.."$upstream_ref" 2>/dev/null || true)"
    if [ -z "$behind" ] || ! [[ "$behind" =~ ^[0-9]+$ ]]; then
        log "[$REPO_NAME] WARN — could not compare HEAD to $upstream_ref"
        return 0
    fi

    if [ "$behind" -eq 0 ]; then
        REPO_UPSTREAM_STATUS="up-to-date"
        REPO_UPSTREAM_LINE="up to date with origin"
    else
        REPO_UPSTREAM_STATUS="behind"
        REPO_UPSTREAM_BEHIND="$behind"
        if [ "$behind" -eq 1 ]; then
            REPO_UPSTREAM_LINE="1 new commit on origin"
        else
            REPO_UPSTREAM_LINE="$behind new commits on origin"
        fi
    fi
    return 0
}

# Print status for one repo. Sets globals: REPO_NAME, REPO_STATUS_WORD (or empty if skipped).
print_repo_status() {
    local repo="$1"
    REPO_NAME="$(basename "$repo")"
    REPO_STATUS_WORD=""

    if [ ! -d "$repo" ]; then
        log "[$REPO_NAME] SKIP — directory does not exist: $repo"
        SKIPPED+=("$REPO_NAME (missing directory)")
        return 1
    fi
    if [ ! -f "$repo/docker-compose.yml" ]; then
        log "[$REPO_NAME] SKIP — no docker-compose.yml in $repo"
        SKIPPED+=("$REPO_NAME (not a bot instance)")
        return 1
    fi
    if ! git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        log "[$REPO_NAME] SKIP — not a git repository: $repo"
        SKIPPED+=("$REPO_NAME (not a git repo)")
        return 1
    fi

    local persistence_file="$repo/data/last_activity.json"
    local status_line checker_rc
    status_line="$(python3 "$IDLE_CHECKER" "$persistence_file" "$IDLE_THRESHOLD_MINUTES")"
    checker_rc=$?
    REPO_STATUS_WORD="${status_line%% *}"
    case "${checker_rc}:${REPO_STATUS_WORD}" in
        0:IDLE|1:ACTIVE|2:UNKNOWN) ;;
        *)
            status_line="UNKNOWN (idle checker returned inconsistent status)"
            REPO_STATUS_WORD="UNKNOWN"
            ;;
    esac

    local running
    running="$(compose "$repo" ps --status running -q 2>/dev/null | wc -l | tr -d ' ')"

    local branch head_info
    branch="$(git -C "$repo" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")"
    head_info="$(git -C "$repo" log -1 --format='%h  %cd  %s' --date=format:'%Y-%m-%d %H:%M' 2>/dev/null)"

    echo ""
    echo "== $REPO_NAME =="
    echo "  path:       $repo"
    echo "  containers: ${running} running"
    echo "  activity:   $status_line"
    echo "  branch:     $branch"
    echo "  local HEAD: ${head_info:-(unknown)}"
    check_upstream_updates "$repo"
    echo "  upstream:   $REPO_UPSTREAM_LINE"
    if [ "$REPO_UPSTREAM_STATUS" = "behind" ] && [ -n "$REPO_UPSTREAM_REF" ]; then
        echo "  to pull:"
        git -C "$repo" log "HEAD..${REPO_UPSTREAM_REF}" \
            --format='    %h  %cd  %s' \
            --date=format:'%Y-%m-%d %H:%M' \
            --reverse 2>/dev/null || echo "    (could not list commits)"
    fi
    return 0
}

# After print_repo_status: ask whether to include this repo in the deploy batch.
decide_deploy() {
    local status_word="$1"

    if [ "$status_word" = "IDLE" ]; then
        if [ "$AUTO_YES" -eq 1 ]; then
            return 0
        fi
        confirm "  Proceed with update for $REPO_NAME?" "y"
        return
    fi

    if [ "$SKIP_ACTIVE" -eq 1 ]; then
        log "[$REPO_NAME] SKIP — $status_word, --skip-active set"
        SKIPPED+=("$REPO_NAME ($status_word)")
        return 1
    fi
    echo "  WARNING: bot looks like it may currently be in use."
    confirm "  Proceed with update for $REPO_NAME anyway?" "n"
}

deploy_repo() (
    local repo="$1" name="$1" target marker cid previous_image override result
    result="$DEPLOY_RESULT_DIR/$(printf '%s' "$repo" | sha256sum | cut -d' ' -f1)"
    finish() { log "[$name] $1 — $2"; printf '%s|%s (%s)\n' "$1" "$name" "$2" >"$result"; }
    exec 9>"$(git -C "$repo" rev-parse --absolute-git-dir)/bookclub-deploy.lock"
    flock -n 9 || { finish skipped "deployment locked"; return; }
    if [ -n "$(git -C "$repo" status --porcelain)" ]; then
        finish skipped "dirty working tree"; return
    fi
    if [ "$(git -C "$repo" branch --show-current)" != main ]; then
        finish skipped "branch is not main"; return
    fi
    git -C "$repo" fetch origin --quiet >>"$LOG_FILE" 2>&1 || { finish failed "fetch failed"; return; }
    target="$(git -C "$repo" rev-parse origin/main)" || { finish failed "missing origin/main"; return; }
    git -C "$repo" merge-base --is-ancestor HEAD "$target" || { finish skipped "local commits or divergence"; return; }
    marker="$(git -C "$repo" rev-parse --absolute-git-dir)/bookclub-deploy.pending"
    if [ "$(git -C "$repo" rev-parse HEAD)" = "$target" ] && [ ! -f "$marker" ]; then
        finish skipped "up to date"; return
    fi
    cid="$(compose "$repo" ps --status running -q bot)" || { finish failed "container status failed"; return; }
    if [ "$AUTOMATIC" -eq 1 ] && [ -z "$cid" ]; then finish skipped "stopped"; return; fi
    if [ "$SKIP_ACTIVE" -eq 1 ] && ! python3 "$IDLE_CHECKER" "$repo/data/last_activity.json" "$IDLE_THRESHOLD_MINUTES" >>"$LOG_FILE"; then
        finish skipped "ACTIVE or UNKNOWN"; return
    fi
    previous_image=""
    if [ -n "$cid" ]; then
        previous_image="$(docker inspect --format '{{.Image}}' "$cid")" || { finish failed "image inspection failed"; return; }
    fi
    printf '%s\n' "$target" >"$marker.tmp" && mv "$marker.tmp" "$marker" || { finish failed "pending marker failed"; return; }
    git -C "$repo" merge --ff-only "$target" >>"$LOG_FILE" 2>&1 || { finish failed "fast-forward failed"; return; }
    log "[$repo] Building $target while current bot remains running"
    compose "$repo" build bot >>"$LOG_FILE" 2>&1 || { finish failed "build failed"; return; }
    if [ -n "$(git -C "$repo" status --porcelain)" ] || [ "$(git -C "$repo" rev-parse HEAD)" != "$target" ]; then
        finish skipped "checkout changed during build"; return
    fi
    if [ "$AUTOMATIC" -eq 1 ]; then
        local current
        current="$(compose "$repo" ps --status running -q bot)" || { finish failed "container recheck failed"; return; }
        if [ -z "$current" ] || [ "$current" != "$cid" ]; then finish skipped "container changed or stopped"; return; fi
    fi
    if [ "$SKIP_ACTIVE" -eq 1 ] && ! python3 "$IDLE_CHECKER" "$repo/data/last_activity.json" "$IDLE_THRESHOLD_MINUTES" >>"$LOG_FILE"; then
        finish skipped "activity changed during build"; return
    fi
    if ! compose "$repo" up -d --no-build --force-recreate --wait --wait-timeout 60 bot >>"$LOG_FILE" 2>&1; then
        if [ -n "$previous_image" ]; then
            override="$(mktemp)"
            printf 'services:\n  bot:\n    image: "%s"\n' "$previous_image" >"$override"
            compose "$repo" -f "$repo/docker-compose.yml" -f "$override" up -d --no-build --force-recreate --wait --wait-timeout 60 bot >>"$LOG_FILE" 2>&1 || log "[$repo] Recovery FAILED; manual intervention required"
            rm -f "$override"
        fi
        finish failed "replacement failed; recovery attempted"; return
    fi
    rm -f "$marker"
    finish updated "$target"
)

log "===== deploy_bots.sh starting (threshold=${threshold_display}, repos=${#REPOS[@]}) ====="

declare -A SEEN=()
for repo in "${REPOS[@]}"; do
    if [ -d "$repo" ]; then repo="$(cd "$repo" && pwd -P)"; fi
    if [ "${SEEN[$repo]:-0}" = 1 ]; then continue; fi
    SEEN[$repo]=1
    if [ "$AUTOMATIC" -eq 1 ] && [ "$CHECK_ONLY" -eq 0 ]; then
        if [ -f "$repo/docker-compose.yml" ] && git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
            TO_DEPLOY+=("$repo")
        else
            FAILED+=("$repo (invalid instance)")
        fi
        continue
    fi
    if ! print_repo_status "$repo"; then
        continue
    fi

    if [ "$CHECK_ONLY" -eq 1 ]; then
        continue
    fi

    case "$REPO_UPSTREAM_STATUS" in
        up-to-date)
            log "[$REPO_NAME] SKIP — already up to date with origin"
            SKIPPED+=("$REPO_NAME (up to date)")
            if [ ! -f "$(git -C "$repo" rev-parse --absolute-git-dir)/bookclub-deploy.pending" ]; then continue; fi
            ;;
        check-failed)
            log "[$REPO_NAME] SKIP — could not determine whether origin has new commits"
            SKIPPED+=("$REPO_NAME (origin check failed)")
            continue
            ;;
        behind)
            ;;
        *)
            log "[$REPO_NAME] SKIP — unknown upstream status"
            SKIPPED+=("$REPO_NAME (origin check failed)")
            continue
            ;;
    esac

    if ! decide_deploy "$REPO_STATUS_WORD"; then
        log "[$REPO_NAME] SKIP — declined ($REPO_STATUS_WORD)"
        SKIPPED+=("$REPO_NAME (declined, $REPO_STATUS_WORD)")
        continue
    fi

    if [ -n "$(git -C "$repo" status --porcelain)" ]; then
        log "[$REPO_NAME] SKIP — working tree has uncommitted changes, refusing to pull"
        SKIPPED+=("$REPO_NAME (dirty working tree)")
        continue
    fi

    TO_DEPLOY+=("$repo")
done

if [ "$CHECK_ONLY" -eq 0 ] && [ "${#TO_DEPLOY[@]}" -gt 0 ]; then
    echo ""
    echo "===== Deploying ${#TO_DEPLOY[@]} instance(s) in parallel ====="
    DEPLOY_RESULT_DIR="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap 'rm -rf "$DEPLOY_RESULT_DIR"' EXIT

    for repo in "${TO_DEPLOY[@]}"; do
        deploy_repo "$repo" &
    done
    wait

    for repo in "${TO_DEPLOY[@]}"; do
        name="$repo"
        result_file="$DEPLOY_RESULT_DIR/$(printf '%s' "$repo" | sha256sum | cut -d' ' -f1)"
        if [ ! -f "$result_file" ]; then
            FAILED+=("$name (no result — interrupted?)")
            continue
        fi
        IFS='|' read -r kind detail <"$result_file"
        case "$kind" in
            updated) UPDATED+=("$detail") ;;
            skipped) SKIPPED+=("$detail") ;;
            failed)  FAILED+=("$detail") ;;
            *)       FAILED+=("$name (unknown result: $kind)") ;;
        esac
    done
fi

echo ""
echo "===== Summary ====="
echo "Updated: ${#UPDATED[@]}  ${UPDATED[*]:-}"
echo "Skipped: ${#SKIPPED[@]}  ${SKIPPED[*]:-}"
echo "Failed:  ${#FAILED[@]}  ${FAILED[*]:-}"

[ "${#FAILED[@]}" -eq 0 ]
