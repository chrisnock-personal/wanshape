#!/bin/sh
# End-to-end test with rootful Docker (issue #4): container mode on a Docker
# bridge network with measured round trips, runtime auto-detection, and a
# host-network Docker container (refused by apply, ports found by apply-host).
# Needs root and Docker; CI runs it. Creates ws-d-* containers and network ws-d,
# and removes them on exit.
#
#   sudo sh tests/integration/docker_mode.sh
set -eu

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 3; }
REPO=$(cd "$(dirname "$0")/../.." && pwd)
IMAGE=${WANSHAPE_IT_IMAGE:-docker.io/library/python:3.12-slim}
STATE=$(mktemp -d)
export PYTHONPATH="$REPO/src" XDG_STATE_HOME="$STATE"
ws() { python3 -m wanshape.cli "$@"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

cleanup() {
    docker rm -f ws-d-a ws-d-b ws-d-h > /dev/null 2>&1 || true
    docker network rm ws-d > /dev/null 2>&1 || true
    rm -rf "$STATE"
}
trap cleanup EXIT
cleanup

ECHO_PY='
import socket
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.bind(("0.0.0.0", 9999))
while True:
    d, a = u.recvfrom(2000); u.sendto(d, a)
'
RTT_PY='
import socket, sys, time
host = sys.argv[1]; samples = []; tries = 0
while len(samples) < 7 and tries < 20:
    tries += 1
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(1)
    t = time.monotonic(); s.sendto(b"x", (host, 9999))
    try:
        s.recv(10); samples.append((time.monotonic() - t) * 1000)
    except TimeoutError:
        pass
    s.close()
print(round(sorted(samples)[len(samples) // 2]) if samples else 9999)
'
expect() {  # expect LABEL MIN MAX
    got=$(docker exec ws-d-b python3 -c "$RTT_PY" "$A_IP")
    if [ "$got" -lt "$2" ] || [ "$got" -gt "$3" ]; then fail "$1: ${got} ms, expected $2..$3"; fi
    echo "ok   $1: ${got} ms"
}

docker network create ws-d > /dev/null
docker run -d --name ws-d-a --network ws-d "$IMAGE" python3 -c "$ECHO_PY" > /dev/null
docker run -d --name ws-d-b --network ws-d "$IMAGE" sleep 600 > /dev/null
A_IP=$(docker inspect ws-d-a --format '{{(index .NetworkSettings.Networks "ws-d").IPAddress}}')
sleep 1

expect "baseline" 0 10
ws apply ws-d-a --runtime docker --delay 30ms > /dev/null
expect "docker bridge, 30 ms each way" 55 90
ws status ws-d-a | grep -q "egress=on ingress=on" || fail "status"
ws clear ws-d-a > /dev/null
expect "cleared" 0 10
ws apply ws-d-a --delay 20ms > /dev/null            # runtime found automatically
expect "runtime auto-detected" 35 70
ws apply ws-d-a,ws-d-b --delay 10ms > /dev/null     # two Docker containers at once
expect "both ends, 10 ms x 2 x 2" 35 70
ws clear ws-d-a ws-d-b > /dev/null
expect "both cleared" 0 10

docker run -d --name ws-d-h --network host -e SIP_PORT=47000 -e RTP_PORT_LOW=47100 \
    -e RTP_PORT_HIGH=47199 "$IMAGE" sleep 600 > /dev/null
set +e; ws apply ws-d-h --delay 10ms > /dev/null 2>&1; code=$?; set -e
[ "$code" -eq 2 ] || fail "host-network Docker container: exit $code, expected 2"
ws apply-host ws-d-h --profile 3g --dry-run | grep -q "ports=47000,47100-47199" \
    || fail "apply-host didn't read the Docker container's ports"
echo "ok   host-network Docker container: refused by apply, ports found by apply-host"
echo "PASS"
