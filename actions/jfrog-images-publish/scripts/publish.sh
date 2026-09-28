#!/usr/bin/env bash
#
# Reconcile one (gcp_project, team) pair's images into Artifactory.
#
# For each image: compare the source digest to the destination digest and copy only
# what is missing or mismatched, then verify what landed.
#
# Both registry logins are the caller's job; this script only reads and copies.
# Exits non-zero if any image fails to copy or verify. Every image is attempted before
# exiting so one bad tag does not mask the state of the rest.

set -uo pipefail

: "${IMAGES_JSON:?IMAGES_JSON is required}"
: "${DEST_REGISTRY:?DEST_REGISTRY is required}"
DEST_PREFIX="${DEST_PREFIX:-}"
DRY_RUN="${DRY_RUN:-false}"
TEAM="${TEAM:-}"
ENVIRONMENT="${ENVIRONMENT:-}"

copied=0
skipped=0
failed=0
planned=0

# One row per image for the job summary, built as we go. Held in memory rather
# than appended to $GITHUB_STEP_SUMMARY directly so the table has a header even
# when the loop copies nothing, and so this script still runs outside Actions.
rows=()

# Digests are long; the summary is for scanning, not for copy-paste verification
# (the full digest is in the per-image log group above).
short_digest() {
  case "$1" in
    sha256:*) echo "${1:7:12}" ;;
    "")       echo "-" ;;
    *)        echo "${1:0:12}" ;;
  esac
}

add_row() {
  # target:tag | outcome | source digest (short)
  rows+=("| \`$1\` | $2 | \`$(short_digest "${3:-}")\` |")
}

# crane digest prints to stdout and errors to stderr; a missing image is an error
remote_digest() {
  crane digest "$1" 2>/dev/null
}

while IFS=$'\t' read -r source target_path tag; do
  [ -n "${source:-}" ] || continue

  if [ -n "$DEST_PREFIX" ]; then
    dest="${DEST_REGISTRY}/${DEST_PREFIX}/${target_path}:${tag}"
  else
    dest="${DEST_REGISTRY}/${target_path}:${tag}"
  fi

  echo "::group::${target_path}:${tag}"
  echo "source:      ${source}"
  echo "destination: ${dest}"

  src_digest="$(remote_digest "$source")"
  if [ -z "$src_digest" ]; then
    echo "::error::cannot read source digest: ${source}"
    failed=$((failed + 1))
    add_row "${target_path}:${tag}" "unreadable at source" ""
    echo "::endgroup::"
    continue
  fi
  echo "source digest: ${src_digest}"

  dst_digest="$(remote_digest "$dest")"
  if [ -z "$dst_digest" ]; then
    echo "decision: COPY (not present at destination)"
    reason="new"
  elif [ "$dst_digest" != "$src_digest" ]; then
    # Tags are immutable by policy, so this means the source tag was re-pushed.
    echo "::warning::tag moved at source -- destination has ${dst_digest}, source has ${src_digest}"
    echo "decision: COPY (digest mismatch)"
    reason="digest mismatch"
  else
    echo "decision: SKIP (already present, digest matches)"
    skipped=$((skipped + 1))
    add_row "${target_path}:${tag}" "already correct" "$src_digest"
    echo "::endgroup::"
    continue
  fi

  if [ "$DRY_RUN" = "true" ]; then
    echo "dry run -- not copying"
    planned=$((planned + 1))
    add_row "${target_path}:${tag}" "would copy (${reason})" "$src_digest"
    echo "::endgroup::"
    continue
  fi

  if ! crane copy "$source" "$dest"; then
    echo "::error::copy failed: ${source} -> ${dest}"
    failed=$((failed + 1))
    add_row "${target_path}:${tag}" "copy failed" "$src_digest"
    echo "::endgroup::"
    continue
  fi

  landed="$(remote_digest "$dest")"
  if [ "$landed" != "$src_digest" ]; then
    echo "::error::verify failed for ${dest}: expected ${src_digest}, got ${landed:-<none>}"
    failed=$((failed + 1))
    add_row "${target_path}:${tag}" "verify failed" "$src_digest"
    echo "::endgroup::"
    continue
  fi

  echo "verified: ${landed}"
  copied=$((copied + 1))
  add_row "${target_path}:${tag}" "copied (${reason})" "$src_digest"
  echo "::endgroup::"
done < <(echo "$IMAGES_JSON" | jq -r '.[] | [.source, .target_path, .tag] | @tsv')

echo
if [ "$DRY_RUN" = "true" ]; then
  echo "DRY RUN summary: ${planned} would copy, ${skipped} already correct, ${failed} unreadable"
else
  echo "summary: ${copied} copied, ${skipped} skipped, ${failed} failed"
fi

# Job summary: one row per image, so the run itself is the audit record of what
# this team has in Artifactory. 
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### ${TEAM:-images}${ENVIRONMENT:+ — $ENVIRONMENT}"
    echo
    if [ "$DRY_RUN" = "true" ]; then
      echo "**Dry run** — nothing was copied."
      echo
      echo "${planned} would copy · ${skipped} already correct · ${failed} unreadable"
    else
      echo "${copied} copied · ${skipped} already correct · ${failed} failed"
    fi
    echo
    echo "| image | outcome | digest |"
    echo "| --- | --- | --- |"
    if [ "${#rows[@]}" -eq 0 ]; then
      echo "| _no images_ | | |"
    else
      printf '%s\n' "${rows[@]}"
    fi
    echo
    echo "<sub>destination: \`${DEST_REGISTRY}${DEST_PREFIX:+/$DEST_PREFIX}\`</sub>"
  } >> "$GITHUB_STEP_SUMMARY"
fi

[ "$failed" -eq 0 ] || exit 1
