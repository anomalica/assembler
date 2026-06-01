#!/usr/bin/env bash
# Generate every Tier 1 node (>= 40 claims) that doesn't already have a page,
# minus a handful of intentional exclusions (codenames that overlap with
# existing event pages, type-duplicates already covered elsewhere).
set -u

declare -a NODES=(
	# people (13 new)
	"Coulthart, Ross"
	"Reid, Harry"
	"DeLonge, Tom"
	"Kean, Leslie"
	"Stratton, Jay"
	"Davis, Eric"
	"Farinaccio, Annie"
	"Whitaker, Bill"
	"Underwood, Chad"
	"Bigelow, Robert"
	"Crosson, Thomas"
	"Day, Kevin"
	"Kobitz, Nat"
	# organisations (7 new)
	"Department of Defense (DoD)"
	"To the Stars Academy of Arts and Science"
	"CIA"
	"The New York Times"
	"Office of Under Secretary of Defense for Intelligence (OUSD(I))"
	"Advanced Aerospace Weapon System Applications Program (AAWSAP)"
	"US Air Force"
	# events (1 new - Tic Tac Sighting excluded as codename)
	"Westall UFO Sighting"
	# matters (6 new)
	"Elizondo Advanced Aerospace Threat Identification Program (AATIP) Claims Investigation"
	"Nimitz UAP Incident, 2004"
	"Grusch Unidentified Anomalous Phenomena (UAP) Whistleblower Disclosure, 2023"
	"Advanced Aerospace Weapon System Applications Program"
	"Pentagon Advanced Aerospace Threat Identification Program (AATIP) UFO Program, 2007-2017"
	"UAP Biological Effects"
	# documents (2 new)
	"Project Blue Book"
	"Gimbal Video"
	# concepts (1 new - bare AAV object excluded as duplicate of AAV concept)
	"Anomalous Aerial Vehicle (AAV)"
)

echo "queued ${#NODES[@]} tier-1 nodes"
ok=0
failed=()
for node in "${NODES[@]}"; do
	echo
	echo "===  [$((ok + ${#failed[@]} + 1))/${#NODES[@]}]  $node  ==="
	if docker run --rm \
		--name "tier1-$(echo "$node" | head -c 24 | tr -c 'a-zA-Z0-9-' -)" \
		-v /home/mark/repos/anomalica/assembler:/work \
		-v /home/mark/.local/share/digester:/db:ro \
		-v /home/mark/repos/anomalica/content:/content \
		-v /home/mark/.local/bin/claude:/usr/local/bin/claude:ro \
		-v /home/mark/.claude:/home/nonroot/.claude \
		-v /home/mark/.claude.json:/home/nonroot/.claude.json \
		-v /tmp/digester-sandbox/empty-CLAUDE.md:/home/nonroot/.claude/CLAUDE.md:ro \
		-v /tmp/digester-sandbox/empty-settings.json:/home/nonroot/.claude/settings.json:ro \
		--user "$(id -u):$(id -g)" --network host -e HOME=/home/nonroot \
		-w /work anomalica-digester:development \
		python assembler.py --node "$node" 2>&1 | tail -4; then
		ok=$((ok + 1))
	else
		failed+=("$node")
	fi
done

echo
echo "==========================="
echo "Tier 1 batch: $ok ok, ${#failed[@]} failed"
if [ ${#failed[@]} -gt 0 ]; then
	echo "failed nodes:"
	printf '  %s\n' "${failed[@]}"
fi

# Build the site once at the end
echo
echo "Building site..."
cd /home/mark/repos/anomalica/site && hugo --gc --minify --quiet 2>&1 || true
ls public/en/people public/en/organisations public/en/events public/en/objects public/en/matters public/en/concepts public/en/documents 2>/dev/null | head -100 || true
