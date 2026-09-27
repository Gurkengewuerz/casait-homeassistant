#!/bin/bash

# script/hooks/yaml-check.pre.sh: Drop a GitHub token that github.com rejects
#
# zizmor audits workflows online whenever GH_TOKEN or GITHUB_TOKEN is set, and
# fails the whole check if github.com refuses that token for git requests - as
# it does for the scoped tokens of sandboxed agent sessions. Without a token it
# runs its offline audits instead, so a rejected token is dropped here.
#
# Sourced by run_hook in script/yaml-check.

for _token_var in GH_TOKEN GITHUB_TOKEN; do
    _token="${!_token_var:-}"
    [[ -n "$_token" ]] || continue
    _status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 -u "x-access-token:${_token}" \
        "https://github.com/actions/checkout.git/info/refs?service=git-upload-pack" || true)
    if [[ "$_status" == "401" || "$_status" == "403" ]]; then
        log_warning "github.com rejects ${_token_var}; auditing workflows offline"
        unset "$_token_var"
    fi
done
unset _token_var _token _status
