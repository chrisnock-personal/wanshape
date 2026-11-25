#!/bin/sh
# End-to-end test of host mode (apply-host / status / clear-host) against the real
# kernel, without root: it runs in a private user + network namespace, where a
# veth pair stands in for the host interface and its peer for the rest of the LAN.
#
#   sh tests/integration/host_mode.sh        (from the repo root)
#
# Needs unprivileged user namespaces, python3, ip and tc. Nothing outside the
# namespaces is touched, and they vanish when the script exits.
set -eu

if [ "${WANSHAPE_IN_NS:-}" != 1 ]; then
    exec env WANSHAPE_IN_NS=1 unshare -Urnm sh "$0" "$@"
fi

REPO=$(cd "$(dirname "$0")/../.." && pwd)
export PYTHONPATH="$REPO/src"
ws() { python3 -m wanshape.cli "$@"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

mount -t tmpfs tmpfs /run
ip link set lo up
ip netns add peer
ip link add h0 type veth peer name p0
ip link set p0 netns peer
ip addr add 10.9.0.1/24 dev h0
ip link set h0 up
ip netns exec peer sh -c 'ip link set lo up; ip addr add 10.9.0.2/24 dev p0; ip addr add 10.9.0.3/24 dev p0; ip link set p0 up'

# UDP echo and a TCP listener on the peer.
ip netns exec peer python3 -c '
import socket, threading, select
# One socket per address, so a reply comes from the address that was asked.
us = []
for addr in ("10.9.0.2", "10.9.0.3"):
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.bind((addr, 9999)); us.append(u)
t = socket.socket(); t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
t.bind(("10.9.0.2", 9998)); t.listen(64)
def tcp():
    while True:
        c, _ = t.accept(); c.close()
threading.Thread(target=tcp, daemon=True).start()
while True:
    for u in select.select(us, [], [])[0]:
        d, a = u.recvfrom(2000); u.sendto(d, a)
' &
PEER=$!
trap 'kill $PEER 2>/dev/null || true' EXIT
sleep 0.3

# Median RTT in ms from a given local port: udp echo or tcp connect.
rtt() {
    python3 - "$1" "$2" <<'EOF'
import socket, struct, sys, time
import os
proto, port = sys.argv[1], int(sys.argv[2])
dest = os.environ.get("DEST", "10.9.0.2")
samples = []
tries = 0
while len(samples) < 7:
    tries += 1
    if tries > 20:
        sys.exit("too many lost probes")
    if proto == "udp":
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("10.9.0.1", port)); s.settimeout(1)
        t = time.monotonic(); s.sendto(b"x", (dest, 9999))
        try:
            s.recv(10)
        except TimeoutError:  # profiles with loss may drop a probe
            s.close()
            continue
    else:
        s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("10.9.0.1", port)); s.settimeout(3)
        # Close with RST so the fixed local port isn't held in TIME_WAIT.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        t = time.monotonic(); s.connect(("10.9.0.2", 9998))
    samples.append((time.monotonic() - t) * 1000)
    s.close()
print(round(sorted(samples)[len(samples) // 2]))
EOF
}
expect() {  # expect LABEL PROTO PORT MIN MAX
    got=$(rtt "$2" "$3")
    if [ "$got" -lt "$4" ] || [ "$got" -gt "$5" ]; then
        fail "$1: $2 from port $3 took ${got} ms, expected $4..$5"
    fi
    echo "ok   $1: $2 from port $3 = ${got} ms"
}

expect "baseline" udp 40001 0 5

ws apply-host sip-a --ports 40000-40010,41000 --iface h0 --delay 60ms > /dev/null
expect "shaped range" udp 40001 115 140          # 60 ms each way
expect "shaped single port" tcp 41000 115 140    # TCP handshake, both directions
expect "unshaped port" udp 50001 0 5

ws apply-host sip-a --ports 40000-40010 --iface h0 --delay 20ms --direction egress > /dev/null
expect "re-applied egress only" udp 40001 15 35
expect "port dropped on re-apply" tcp 41000 0 5

if ws apply-host sip-b --ports 40005-40020 --iface h0 --delay 5ms > /dev/null 2>&1; then
    fail "overlapping ports were accepted"
fi
echo "ok   overlapping ports refused"

ws apply-host sip-b --ports 42000 --iface h0 --profile 4g > /dev/null
expect "second target" udp 42000 30 80           # 25 ms each way, 10 ms jitter
ws status | grep -q "sip-b: host mode, .*profile=4g" || fail "status does not show sip-b"
echo "ok   status lists both targets"

ws clear-host sip-a > /dev/null
expect "first target cleared" udp 40001 0 5
tc qdisc show dev h0 | grep -q clsact || fail "clsact removed while sip-b still uses it"
echo "ok   clsact kept for the remaining target"

ws clear-host --all > /dev/null
expect "all cleared" udp 42000 0 5
tc qdisc show dev h0 | grep -q clsact && fail "clsact left behind"
ip link show type ifb | grep -q wsh && fail "IFB left behind"
echo "ok   nothing left behind"
# Clients on random local ports (#11): match the remote port, optionally one peer.
ws apply-host ws-client --remote-ports 9999 --iface h0 --delay 40ms > /dev/null
expect "remote port 9999 from a random local port" udp 0 75 100
DEST=10.9.0.3 expect "...to another host too" udp 0 75 100
ws apply-host ws-client --remote-ports 9999 --peer 10.9.0.2 --iface h0 --delay 40ms > /dev/null
expect "peer 10.9.0.2 shaped" udp 0 75 100
DEST=10.9.0.3 expect "other host not shaped" udp 0 0 5
ws apply-host ws-client --peer 10.9.0.3 --iface h0 --delay 25ms > /dev/null
DEST=10.9.0.3 expect "peer only: all traffic with 10.9.0.3" udp 0 45 65
expect "peer only: 10.9.0.2 not shaped" udp 0 0 5
ws status | grep -q "ws-client: host mode, iface=h0 peer=10.9.0.3" || fail "status for a peer target"
ws clear-host ws-client > /dev/null
echo "ok   remote ports and peer"

# More netem options (#10): every packet duplicated means two echoes per probe.
ws apply-host dup --ports 43000 --iface h0 --delay 1ms --duplicate 100% --direction egress \
    > /dev/null
got=$(python3 -c '
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("10.9.0.1", 43000)); s.settimeout(0.5)
s.sendto(b"x", ("10.9.0.2", 9999)); n = 0
try:
    while True:
        s.recv(10); n += 1
except TimeoutError:
    pass
print(n)')
[ "$got" -eq 2 ] || fail "duplicate 100%: got $got echoes, expected 2"
ws clear-host dup > /dev/null
echo "ok   --duplicate 100% doubles the packets"
echo "PASS"
