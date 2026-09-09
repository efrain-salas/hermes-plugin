#!/usr/bin/env bash
set -euo pipefail

HERMES=/opt/hermes/.venv/bin/hermes
mkdir -p /opt/data

if [ ! -d /opt/data/profiles/mujer ]; then
  "$HERMES" profile create mujer --no-skills
fi

configure_profile() {
  local profile_flag=("$@")
  "$HERMES" "${profile_flag[@]}" config set model.default mock-model
  "$HERMES" "${profile_flag[@]}" config set model.provider mock
  "$HERMES" "${profile_flag[@]}" config set providers.mock.api http://fake-llm:8081/v1
  "$HERMES" "${profile_flag[@]}" config set providers.mock.api_mode chat_completions
  "$HERMES" "${profile_flag[@]}" config set providers.mock.key_env MOCK_API_KEY
  "$HERMES" "${profile_flag[@]}" config set providers.mock.models.mock-model '{}'
  "$HERMES" "${profile_flag[@]}" config set providers.mock.context_length 4096
  "$HERMES" "${profile_flag[@]}" config set MOCK_API_KEY e2e-mock-key
}

configure_profile
configure_profile -p mujer

# Use Hermes' public plugin lifecycle so discovery, consent and per-profile
# activation follow the same path as a production installation.
printf 'n\n' | "$HERMES" plugins enable hermes-mobile
printf 'n\n' | "$HERMES" -p mujer plugins enable hermes-mobile

"$HERMES" config set API_SERVER_KEY default-api-server-key-0000000000000001
"$HERMES" -p mujer config set API_SERVER_KEY mujer-api-server-key-00000000000000002
"$HERMES" config set gateway.multiplex_profiles true
"$HERMES" config set gateway.multiplex_profile_allowlist '["mujer"]'
"$HERMES" config set plugins.entries.hermes-mobile.settings.loopback_base_url http://127.0.0.1:8642
"$HERMES" config set plugins.entries.hermes-mobile.settings.push.enabled true
"$HERMES" config set plugins.entries.hermes-mobile.settings.push.endpoint http://fake-expo:8082/--/api/v2/push/send
"$HERMES" config set plugins.entries.hermes-mobile.settings.push.timeout_seconds 1
"$HERMES" config set plugins.entries.hermes-mobile.settings.push.max_attempts 3

"$HERMES" mobile provision
"$HERMES" mobile doctor --profile default > /opt/data/test-doctor-default.json
"$HERMES" mobile doctor --profile mujer > /opt/data/test-doctor-mujer.json
"$HERMES" mobile pair --profile default --display-name Default --json \
  > /opt/data/test-pair-default.json
"$HERMES" mobile pair --profile mujer --display-name Mujer --json \
  > /opt/data/test-pair-mujer.json

chown -R 10000:10000 /opt/data
