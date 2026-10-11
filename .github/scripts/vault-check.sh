#!/usr/bin/env bash
# Usage: vault-check.sh <case-id> <role> <audience> [environment]
# Logs in to Vault with this job's GitHub OIDC token, then probes access.
# The EXPECTED outcome is computed from repo/ref/environment/audience (oracle below),
# so the same file runs in every repo/branch and each run self-verifies.
# A correctly DENIED login is a PASS. Exit 1 on any mismatch.
# Keep the oracle in sync with instances/test/issuers/github-actions.yaml.
set -uo pipefail
CASE=$1 ROLE=$2 AUD=$3 JOB_ENV=${4:-}
: "${VAULT_ADDR:?}" "${ACTIONS_ID_TOKEN_REQUEST_URL:?need permissions: id-token: write}"
MOUNT=${MOUNT:-github-actions} KV=${KV:-rubrik-secret} PREFIX=${PREFIX:-infosec/test}
OWNER=swastik-rubrik
GOOD_AUD=https://github.com/$OWNER
declare -A REPO=([gh-main]=$OWNER/gh-workflows-swastik [gh-env-prod]=$OWNER/gh-workflows-swastik [poc-main]=$OWNER/poc)
declare -A OWN=([gh-main]=gh-test [gh-env-prod]=gh-test-prod [poc-main]=poc)
FAILS=0; ROWS=""

row() { # row <check> <expect> <got>
  local r=PASS; [[ "$2" == "$3" ]] || { r=FAIL; FAILS=$((FAILS+1)); }
  ROWS+="| $1 | $2 | $3 | $r |"$'\n'; echo "$r  $1 expect=$2 got=$3"; }
code() { [[ "$1" == 200 || "$1" == 204 ]] && echo ALLOW || echo DENY; }

# --- oracle: should this login succeed? ---
EXPECT=DENY
if [[ -n "${REPO[$ROLE]:-}" && "$GITHUB_REPOSITORY" == "${REPO[$ROLE]}" && "$GITHUB_REF" == refs/heads/main && "$AUD" == "$GOOD_AUD" ]]; then
  if [[ "$ROLE" != gh-env-prod || "$JOB_ENV" == prod ]]; then EXPECT=ALLOW; fi
fi

# --- get OIDC token with requested audience ---
JWT=$(curl -sS -G -H "Authorization: Bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" --data-urlencode "audience=$AUD" "$ACTIONS_ID_TOKEN_REQUEST_URL" | jq -r .value)
echo "::add-mask::$JWT"
CLAIMS=$(cut -d. -f2 <<<"$JWT" | tr '_-' '/+' | base64 -d 2>/dev/null | jq -c '{iss,aud,sub,repository,repository_owner,ref,environment,job_workflow_ref}')

# --- login ---
RESP=$(curl -sS -X PUT -d "$(jq -cn --arg r "$ROLE" --arg j "$JWT" '{role:$r,jwt:$j}')" "$VAULT_ADDR/v1/auth/$MOUNT/login")
TOKEN=$(jq -r '.auth.client_token // empty' <<<"$RESP"); [[ -n "$TOKEN" ]] && echo "::add-mask::$TOKEN"
GOT=DENY; [[ -n "$TOKEN" ]] && GOT=ALLOW
row "login as \`$ROLE\`" "$EXPECT" "$GOT"
ERR=$(jq -r '.errors[0] // ""' <<<"$RESP")

rd() { code "$(curl -s -o /dev/null -w '%{http_code}' -H "X-Vault-Token: $TOKEN" "$VAULT_ADDR/v1/$KV/data/$PREFIX/$1")"; }
wr() { code "$(curl -s -o /dev/null -w '%{http_code}' -H "X-Vault-Token: $TOKEN" -d '{"data":{"v":"x"}}' "$VAULT_ADDR/v1/$KV/data/$PREFIX/$1")"; }

if [[ "$GOT" == ALLOW ]]; then
  row "read own  $PREFIX/${OWN[$ROLE]}/db" ALLOW "$(rd "${OWN[$ROLE]}/db")"
  for o in gh-test gh-test-prod poc other; do
    [[ "$o" == "${OWN[$ROLE]}" ]] && continue
    row "read      $PREFIX/$o/db (not granted)" DENY "$(rd "$o/db")"
  done
  if [[ "$ROLE" == gh-main ]]; then
    row "write     $PREFIX/gh-test/ci/x" ALLOW "$(wr gh-test/ci/x)"
    row "write     $PREFIX/gh-test/db (outside ci/)" DENY "$(wr gh-test/db)"
  else
    row "write     $PREFIX/${OWN[$ROLE]}/ci/x (read-only role)" DENY "$(wr "${OWN[$ROLE]}/ci/x")"
  fi
fi

{
  echo "### Case $CASE: role \`$ROLE\`, aud \`$AUD\`, env \`${JOB_ENV:-none}\`"
  echo "Repo \`$GITHUB_REPOSITORY\` @ \`$GITHUB_REF\` (event: $GITHUB_EVENT_NAME)"
  echo '```'; echo "$CLAIMS"; echo '```'
  [[ -n "$ERR" ]] && echo "Vault said: \`$ERR\`"
  echo "| Check | Expected | Actual | Result |"; echo "|---|---|---|---|"; printf '%s' "$ROWS"; echo
} >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
exit $(( FAILS > 0 ))
