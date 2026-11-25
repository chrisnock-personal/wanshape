#!/bin/sh
# End-to-end test of VM mode (apply-vm / status / clear-vm) with a real KVM
# guest, without root: QEMU runs in a private user + network namespace on a tap
# device (vnet0) enslaved to a bridge, the layout libvirt uses for its NAT and
# bridge networks. A stand-in `virsh` reports that tap for domain "testvm",
# so everything after the libvirt lookup is the real code path.
#
#   WANSHAPE_VM_IMAGE=/path/to/cirros-0.6.2-x86_64-disk.img sh tests/integration/vm_mode.sh
#
# Any small image whose guest brings its NIC up will do (CirrOS:
# https://download.cirros-cloud.net/). Pings go to the guest's IPv6 link-local
# address, so no DHCP is needed. Needs /dev/kvm, qemu-system-x86_64, ip, tc.
set -eu

: "${WANSHAPE_VM_IMAGE:?set WANSHAPE_VM_IMAGE to a guest disk image (e.g. CirrOS)}"
if [ "${WANSHAPE_IN_NS:-}" != 1 ]; then
    exec env WANSHAPE_IN_NS=1 unshare -Urnm sh "$0" "$@"
fi

REPO=$(cd "$(dirname "$0")/../.." && pwd)
WORK=$(mktemp -d)
export PYTHONPATH="$REPO/src" PATH="$WORK/bin:$PATH"
ws() { python3 -m wanshape.cli "$@"; }
fail() { echo "FAIL: $*" >&2; exit 1; }
MAC=52:54:00:12:34:56
GUEST=fe80::5054:ff:fe12:3456%br0

mkdir -p "$WORK/bin"
cat > "$WORK/bin/virsh" <<EOF
#!/bin/sh
# Stand-in for: virsh -c URI -q domiflist testvm
case "\$*" in
    *"domiflist testvm2"*) echo " vnet1   network   default   virtio   52:54:00:12:34:57" ;;
    *"domiflist testvm"*) echo " vnet0   network   default   virtio   $MAC" ;;
    *) echo "error: failed to get domain" >&2; exit 1 ;;
esac
EOF
chmod +x "$WORK/bin/virsh"

mount -t tmpfs tmpfs /run
ip link set lo up
ip link add br0 type bridge
ip link set br0 up
ip tuntap add vnet0 mode tap
ip link set vnet0 address fe:54:00:12:34:56   # as libvirt does: guest MAC, first byte fe
ip link set vnet0 master br0
ip link set vnet0 up
qemu-system-x86_64 -enable-kvm -m 256 -snapshot -drive "file=$WANSHAPE_VM_IMAGE,if=virtio" \
    -netdev tap,id=n0,ifname=vnet0,script=no,downscript=no \
    -device "virtio-net-pci,netdev=n0,mac=$MAC" \
    -display none -serial "file:$WORK/console.log" -pidfile "$WORK/qemu.pid" &
trap 'kill $(cat "$WORK/qemu.pid" 2>/dev/null) 2>/dev/null || true; rm -rf "$WORK"' EXIT

for i in $(seq 1 120); do ping -c1 -W1 "$GUEST" > /dev/null 2>&1 && break; sleep 1; done
ping -c1 -W1 "$GUEST" > /dev/null 2>&1 || { tail -20 "$WORK/console.log"; fail "guest never answered"; }

rtt() { ping -c 6 -i 0.3 -q "$GUEST" | awk -F/ '/rtt/ {printf "%d", $5}'; }
expect() {  # expect LABEL MIN MAX
    got=$(rtt)
    if [ "$got" -lt "$2" ] || [ "$got" -gt "$3" ]; then fail "$1: ${got} ms, expected $2..$3"; fi
    echo "ok   $1: ${got} ms"
}
drops() { tc -s qdisc show dev "$1" | sed -n 's/.*dropped \([0-9]*\).*/\1/p' | head -1; }

expect "baseline" 0 5
ws apply-vm testvm --delay 40ms > /dev/null
expect "40 ms each way" 75 95
ws status | grep -q "testvm: vm mode, device=vnet0" || fail "status does not show testvm"
echo "ok   status shows the VM"

# Direction mapping: "egress" is the VM's upload, which arrives on the tap's ingress hook.
ws apply-vm testvm --delay 1ms --loss 100% --direction egress > /dev/null
ping -c 3 -W 1 -q "$GUEST" > /dev/null 2>&1 && fail "pings answered with 100% upload loss"
[ "$(drops wsh0up)" -gt 0 ] || fail "upload loss not on wsh0up"
echo "ok   --direction egress drops the VM's replies (wsh0up dropped $(drops wsh0up))"
ws apply-vm testvm --delay 1ms --loss 100% --direction ingress > /dev/null
ping -c 3 -W 1 -q "$GUEST" > /dev/null 2>&1 && fail "pings answered with 100% download loss"
[ "$(drops wsh0dn)" -gt 0 ] || fail "download loss not on wsh0dn"
echo "ok   --direction ingress drops traffic to the VM (wsh0dn dropped $(drops wsh0dn))"

ws clear-vm testvm > /dev/null
expect "cleared" 0 5
tc qdisc show dev vnet0 | grep -q clsact && fail "clsact left behind"
ip link show type ifb | grep -q wsh && fail "IFB left behind"
echo "ok   nothing left behind"

tc qdisc add dev vnet0 ingress   # what libvirt's <bandwidth><outbound> adds
set +e; ws apply-vm testvm --delay 10ms > /dev/null 2>&1; code=$?; set -e
[ "$code" -eq 4 ] || fail "apply over an ingress qdisc gave exit $code, expected 4"
tc qdisc del dev vnet0 ingress
echo "ok   refuses to fight libvirt's own ingress qdisc"

set +e; ws apply-vm nosuchvm --delay 10ms > /dev/null 2>&1; code=$?; set -e
[ "$code" -eq 1 ] || fail "unknown VM gave exit $code"
echo "ok   unknown VM refused"

# Several VMs in one command (#6). testvm2 is a bare tap with no guest.
ip tuntap add vnet1 mode tap
ip link set vnet1 address fe:54:00:12:34:57
ip link set vnet1 master br0
ip link set vnet1 up
ws apply-vm testvm,testvm2 --delay 30ms > /dev/null
[ "$(ws status | grep -c 'vm mode')" -eq 2 ] || fail "two VM targets expected"
ws status | grep -q "testvm2: vm mode, device=vnet1" || fail "testvm2 not on vnet1"
ws clear-vm testvm testvm2 > /dev/null
ip link show type ifb | grep -q wsh && fail "IFBs left after clearing two VMs"
echo "ok   two VMs in one command, cleared together"

# A VM restart recreates the tap under a new name: the old target goes stale.
ws apply-vm testvm --delay 20ms > /dev/null
ip link del vnet0
ws status | grep -q "testvm: .*STALE" || fail "stale target not reported"
ws clear-vm testvm > /dev/null
ip link show type ifb | grep -q wsh && fail "IFBs left after clearing a stale target"
echo "ok   stale target reported and cleared"
echo "PASS"
