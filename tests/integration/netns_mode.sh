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
    # Dependents first: ws-it-s shares ws-it-a's network namespace.
    podman rm -f -t 0 ws-it-s ws-it-q1 ws-it-q2 > /dev/null 2>&1 || true
    podman pod rm -f ws-it-pod > /dev/null 2>&1 || true
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

echo "# status --all / --prune"
ws apply ws-it-a --delay 30ms > /dev/null
rm -f "$STATE/wanshape/state.json"            # state lost: plain status can't see it
ws status | grep -q "ws-it-a" && fail "plain status found ws-it-a without state"
ws status --all | grep -q "ws-it-a: profile=unknown egress=on ingress=on" \
    || fail "status --all did not find the unrecorded shaping"
echo "ok   status --all finds shaping the state file doesn't know about"
ws clear ws-it-a > /dev/null
expect "cleared without state" ws-it-b "$A_IP" 9999 0 10
ws apply ws-it-b --delay 5ms --direction egress > /dev/null
podman rm -f -t 0 ws-it-b > /dev/null
ws status --prune 2>&1 | grep -q "pruned ws-it-b: container is gone" || fail "prune"
ws status 2>&1 | grep -q "ws-it-b" && fail "pruned entry still listed"
echo "ok   status --prune forgets a removed container"
run ws-it-b --network ws-it                     # the probe client again

echo "# shared network namespaces"
podman run -d --init --name ws-it-s --network container:ws-it-a "$IMAGE" sleep 600 > /dev/null
ws apply ws-it-s --delay 30ms 2>&1 >/dev/null | grep -q "shares its network namespace with ws-it-a" \
    || fail "no warning for --network container:"
expect "shaping ws-it-s shapes ws-it-a" ws-it-b "$A_IP" 9999 55 90
ws status ws-it-a | grep -q "egress=on ingress=on" || fail "status via the other member"
[ "$(ws status | grep -c 'profile=')" -eq 1 ] || fail "shared namespace listed twice"
ws clear ws-it-a > /dev/null
expect "cleared through the other member" ws-it-b "$A_IP" 9999 0 10
[ "$(ws status)" = "no wanshape state found" ] || fail "state left after clearing a member"
podman rm -f -t 0 ws-it-s > /dev/null
podman pod create --name ws-it-pod --network ws-it > /dev/null
# Pod members share one namespace, so only one of them can bind the echo port.
run ws-it-q1 --pod ws-it-pod
podman run -d --init --name ws-it-q2 --pod ws-it-pod "$IMAGE" sleep 600 > /dev/null
ws apply ws-it-q1 --delay 10ms 2>&1 >/dev/null | grep -q "shares its network namespace with.*ws-it-q2" \
    || fail "no warning for a pod"
ws clear ws-it-q2 > /dev/null
echo "ok   --network container: and pods: warned, one state entry, clear via any member"

echo "# --duration"
ws apply ws-it-a --delay 30ms --duration 5s | grep -q "clears in" || fail "no time limit reported"
ws status ws-it-a | grep -q "(clears in " || fail "status doesn't show the time limit"
expect "shaped until the timer fires" ws-it-b "$A_IP" 9999 55 90
for i in $(seq 1 30); do
    ws status ws-it-a | grep -q "egress=off ingress=off" && break; sleep 1
done
ws status ws-it-a | grep -q "egress=off ingress=off" || fail "the timer did not clear the shaping"
expect "cleared by the timer" ws-it-b "$A_IP" 9999 0 10
systemctl --user list-timers --all | grep -q wanshape-clear-container-ws-it-a && fail "timer left behind"
ws apply ws-it-a --delay 30ms --duration 1h > /dev/null
ws clear ws-it-a > /dev/null
systemctl --user list-timers --all | grep -q wanshape-clear-container-ws-it-a && fail "clear left the timer"
echo "ok   --duration clears by itself, and clear cancels the timer"

echo "# several targets (#6)"
ws apply ws-it-a,ws-it-b --delay 15ms > /dev/null
expect "both ends shaped: 15 ms x 2 ends x 2 ways" ws-it-b "$A_IP" 9999 55 85
set +e; ws apply ws-it-a no-such-container --delay 40ms > /dev/null 2>&1; code=$?; set -e
[ "$code" -ne 0 ] || fail "unknown name in a list was accepted"
expect "unknown name: nothing changed" ws-it-b "$A_IP" 9999 55 85
ws clear ws-it-a ws-it-b > /dev/null
expect "both cleared" ws-it-b "$A_IP" 9999 0 10
echo "ok   several containers in one command, checked before any change"

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
