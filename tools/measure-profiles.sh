#!/bin/sh
# Measure what each profile does to an RTP-like UDP stream (50 packets/s, 20 s),
# in a private user + network namespace: no root needed, nothing outside is touched.
#
#   sh tools/measure-profiles.sh [profile ...]     (default: all profiles)
#
# Prints, per profile: median and 95th-percentile round trip, and the share of
# probes that never came back (loss in both directions combined).
set -eu
if [ "${WANSHAPE_IN_NS:-}" != 1 ]; then
    exec env WANSHAPE_IN_NS=1 unshare -Urnm sh "$0" "$@"
fi
REPO=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$REPO/src"
ws() { python3 -m wanshape.cli "$@"; }
mount -t tmpfs tmpfs /run
ip link set lo up
ip netns add peer
ip link add h0 type veth peer name p0
ip link set p0 netns peer
ip addr add 10.9.0.1/24 dev h0; ip link set h0 up
ip netns exec peer sh -c 'ip link set lo up; ip addr add 10.9.0.2/24 dev p0; ip link set p0 up'
ip netns exec peer python3 -c '
import socket
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.bind(("10.9.0.2", 9999))
while True:
    d, a = u.recvfrom(2000); u.sendto(d, a)' &
trap 'kill $! 2>/dev/null || true' EXIT
sleep 0.3
PROFILES=${*:-$(ws list | awk '{print $1}')}
printf '%-14s %9s %9s %7s\n' profile "median" "p95" "lost"
for p in $PROFILES; do
    ws apply-host m --ports 40000 --iface h0 --profile "$p" > /dev/null
    python3 - "$p" <<'PY'
import socket, sys, time, select
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("10.9.0.1", 40000))
s.setblocking(False)
sent, rtts, n, start = {}, [], 1000, time.monotonic()
for i in range(n):
    t = start + i * 0.02
    while True:
        now = time.monotonic()
        r, _, _ = select.select([s], [], [], max(0, t - now))
        for _ in range(64):
            try:
                d, _ = s.recvfrom(64)
            except BlockingIOError:
                break
            k = int(d)
            if k in sent:
                rtts.append((time.monotonic() - sent.pop(k)) * 1000)
        if time.monotonic() >= t:
            break
    s.sendto(str(i).encode(), ("10.9.0.2", 9999)); sent[i] = time.monotonic()
end = time.monotonic() + 3
while time.monotonic() < end and sent:
    r, _, _ = select.select([s], [], [], 0.1)
    if r:
        d, _ = s.recvfrom(64)
        k = int(d)
        if k in sent:
            rtts.append((time.monotonic() - sent.pop(k)) * 1000)
rtts.sort()
med = rtts[len(rtts) // 2] if rtts else float("nan")
p95 = rtts[int(len(rtts) * 0.95)] if rtts else float("nan")
print(f"{sys.argv[1]:<14} {med:7.1f}ms {p95:7.1f}ms {100 * len(sent) / n:6.1f}%")
PY
    ws clear-host m > /dev/null
done
