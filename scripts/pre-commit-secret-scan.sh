#!/usr/bin/env bash
# Pre-commit secret scan.
#
# Install (once, per clone — git does not ship hooks on clone):
#     ln -sf ../../scripts/pre-commit-secret-scan.sh .git/hooks/pre-commit
#
# Blocks a commit that would introduce a credential-shaped string. This is a
# safety net, not a substitute for keeping secrets in .env: it only sees
# patterns it knows about.
#
# Bypass (only when you are certain it is a false positive):
#     git commit --no-verify

set -uo pipefail

# Only scan what is actually being committed, and only added lines.
staged_diff=$(git diff --cached --no-color -U0 | grep '^+' | grep -v '^+++' || true)
[ -z "$staged_diff" ] && exit 0

fail=0
report() {
    printf '\n  BLOCKED — %s\n' "$1"
    printf '    %s\n' "$2"
    fail=1
}

# Telegram bot token: 9-10 digits, colon, 35 chars
if echo "$staged_diff" | grep -qE '[0-9]{9,10}:[A-Za-z0-9_-]{35}'; then
    report "Telegram bot token" "Rotate it via @BotFather, then put it in .env"
fi

# MEXC / common exchange API keys
if echo "$staged_diff" | grep -qE '\bmx0[A-Za-z0-9]{18,}\b'; then
    report "MEXC API key" "Revoke it in the MEXC console, then put it in .env"
fi

# Generic provider keys
if echo "$staged_diff" | grep -qE '\bsk-[A-Za-z0-9]{20,}\b|\bAKIA[0-9A-Z]{16}\b'; then
    report "API key (sk-… or AWS AKIA…)" "Revoke it, then put it in .env"
fi

# Private key material
if echo "$staged_diff" | grep -qE -- '-----BEGIN [A-Z ]*PRIVATE KEY-----'; then
    report "private key block" "Never commit key material; keep it outside the repo"
fi

# A non-empty secret-looking assignment in a tracked config file
if echo "$staged_diff" | grep -qiE '^\+[[:space:]]*(token|chat_id|api_key|api_secret|password|secret)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{8,}'; then
    report "hardcoded credential assignment" "Read it from os.getenv() and keep the value in .env"
fi

# .env itself
if git diff --cached --name-only | grep -qE '(^|/)\.env$'; then
    report ".env is staged" "It must stay untracked — git rm --cached .env"
fi

if [ "$fail" -ne 0 ]; then
    printf '\n  Commit aborted. Nothing was committed.\n'
    printf '  If this is genuinely a false positive: git commit --no-verify\n\n'
    exit 1
fi

exit 0
