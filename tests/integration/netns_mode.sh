#!/bin/sh
# End-to-end test of per-container shaping (apply / status / clear) with real
# rootless Podman containers: a netavark bridge pair, a default pasta container
# and a --userns=keep-id container. Run as the normal user, from the repo root:
#
#   sh tests/integration/netns_mode.sh
#
# Uses the local python:3.12-slim image (set WANSHAPE_IT_IMAGE to change it).
# Creates containers ws-it-* and network ws-it, and removes them on exit.
set -eu

REPO=$(cd "$(dirname "$0")/../.." && pwd)
IMAGE=${WANSHAPE_IT_IMAGE:-docker.io/library/python:3.12-slim}
STATE=$(mktemp -d)
export PYTHONPATH="$REPO/src" XDG_STATE_HOME="$STATE"
ws() { python3 -m wanshape.cli "$@"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

cleanup() {
    podman rm -f -t 0 ws-it-a ws-it-b ws-it-p ws-it-k > /dev/null 2>&1 || true
    podman network rm ws-it > /dev/null 2>&1 || true
    [ -n "${ECHO:-}" ] && kill "$ECHO" 2> /dev/null || true
    rm -rf "$STATE"
}
trap cleanup EXIT
cleanup

ECHO_PY='
import socket, sys
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.bind(("0.0.0.0", int(sys.argv[1])))
while True:
    d, a = u.recvfrom(2000); u.sendto(d, a)
'
# Median UDP RTT in ms, measured from inside container $1 to $2:$3.
RTT_PY='
import socket, sys, time
host, port = sys.argv[1], int(sys.argv[2])
samples, tries = [], 0
while len(samples) < 7 and tries < 20:
    tries += 1
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(1)
    t = time.monotonic(); s.sendto(b"x", (host, port))
    try:
        s.recv(10); samples.append((time.monotonic() - t) * 1000)
    except TimeoutError:
        pass
    s.close()
print(round(sorted(samples)[len(samples) // 2]) if samples else 9999)
'
expect() {  # expect LABEL CONTAINER HOST PORT MIN MAX
    got=$(podman exec "$2" python3 -c "$RTT_PY" "$3" "$4")
    if [ "$got" -lt "$5" ] || [ "$got" -gt "$6" ]; then
        fail "$1: ${got} ms, expected $5..$6"
    fi
    echo "ok   $1: ${got} ms"
}
run() { podman run -d --init --name "$@" "$IMAGE" python3 -c "$ECHO_PY" 9999 > /dev/null; }

podman network create ws-it > /dev/null
run ws-it-a --network ws-it
run ws-it-b --network ws-it
A_IP=$(podman inspect ws-it-a --format '{{(index .NetworkSettings.Networks "ws-it").IPAddress}}')

echo "# bridge (netavark)"
expect "baseline" ws-it-b "$A_IP" 9999 0 10
ws apply ws-it-a --delay 50ms > /dev/null
expect "50 ms each way" ws-it-b "$A_IP" 9999 95 130
ws status ws-it-a | grep -q "egress=on ingress=on" || fail "status does not show both directions"
echo "ok   status shows both directions"
ws apply ws-it-a --delay 20ms --no-reorder > /dev/null
expect "re-applied 20 ms" ws-it-b "$A_IP" 9999 35 70
ws apply ws-it-a --delay 20ms --direction ingress > /dev/null
ws status ws-it-a | grep -q "ingress=on" || fail "ingress lost on re-apply"
ws clear ws-it-a > /dev/null
expect "cleared" ws-it-b "$A_IP" 9999 0 10
ws status ws-it-a | grep -q "egress=off ingress=off" || fail "shaping left after clear"
echo "ok   nothing left after clear"

echo "# pasta (rootless default)"
python3 -c "$ECHO_PY" 39999 &
ECHO=$!
run ws-it-p
sleep 0.5
expect "baseline" ws-it-p host.containers.internal 39999 0 15
ws apply ws-it-p --delay 50ms --direction egress > /dev/null
expect "50 ms egress" ws-it-p host.containers.internal 39999 45 80
ws clear ws-it-p > /dev/null
expect "cleared" ws-it-p host.containers.internal 39999 0 15

echo "# --userns=keep-id"
run ws-it-k --network ws-it --userns keep-id
expect "baseline" ws-it-k "$A_IP" 9999 0 10
ws apply ws-it-k --delay 40ms --direction egress > /dev/null
expect "40 ms egress" ws-it-k "$A_IP" 9999 35 70
ws clear ws-it-k > /dev/null

echo "# refusals"
set +e
ws apply ws-it-a --profile no-such-profile > /dev/null 2>&1; [ $? -eq 1 ] || fail "unknown profile"
podman run -d --init --name ws-it-h --network host "$IMAGE" sleep 600 > /dev/null
ws apply ws-it-h --delay 10ms > /dev/null 2>&1; code=$?
podman rm -f -t 0 ws-it-h > /dev/null
set -e
[ "$code" -eq 2 ] || fail "host-network container gave exit $code, expected 2"
echo "ok   unknown profile and host networking refused"
echo "PASS"
