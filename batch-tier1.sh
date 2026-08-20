#!/usr/bin/env bash
# Assemble the Tier 1 node pages (tier1-nodes.txt) via the gated batch driver on
# the metered Anthropic API. Runs in the digester container (python + anthropic).
#
# Prints a spend estimate and stops by default. Pass --confirm to generate:
#   ./batch-tier1.sh            # estimate only, no spend
#   ./batch-tier1.sh --confirm  # generate (after the amount is cleared)
# Extra flags pass through to batch.py (e.g. --model haiku).
set -euo pipefail

cd "$(dirname "$0")"

SOPS="$(command -v sops || echo "$HOME/.nix-profile/bin/sops")"
export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-$HOME/.config/sops/age/keys.txt}"
ANTHROPIC_API_KEY="$("$SOPS" -d --extract '["ANTHROPIC_API_KEY"]' "$HOME/repos/secrets/store/anomalica.yaml")"
export ANTHROPIC_API_KEY

docker run --rm \
	-v /home/mark/repos/anomalica/assembler:/work \
	-v /home/mark/.local/share/assimilator:/db:ro \
	-v /home/mark/repos/anomalica/content:/content \
	-v /home/mark/repos/anomalica/digests:/digests:ro \
	--user "$(id -u):$(id -g)" --network host -e HOME=/home/nonroot \
	-e ANTHROPIC_API_KEY \
	-w /work anomalica-digester:development \
	python batch.py --nodes-file tier1-nodes.txt "$@"
