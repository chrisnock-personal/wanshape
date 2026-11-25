# Phase 0 spike results — laptop, 2026-11-25

Environment: laptop, Fedora 45 kernel `7.3.3-300.fc45.x86_64`, rootless Podman 6.1.3 with crun,
iproute2 (`tc`) 7.0.0. The PC's WSL was unreachable on port 2222, so these results are **laptop-only**;
the WSL2 kernel still has to be checked separately (design §12, item 1).

Throwaway containers: `wanshape-spike-pasta` (default pasta), `wanshape-spike-br` (user-defined
netavark bridge), `wanshape-spike-keep` (`--userns=keep-id`, pasta). All were removed after the spike,
along with the `wanshape-spike-br` network.

## Kernel modules

`sch_netem`, `ifb`, `act_mirred` and `cls_matchall` were **not** loaded at the start and there is no
passwordless sudo, but the kernel autoloaded them on demand when `tc qdisc ... netem`, `ip link add
type ifb` and the `mirred`/`matchall` filter were used. `cls_u32` was not exercised (it is only needed
for the optional host port-filter mode). No custom kernel or manual `modprobe` was needed here.

## Namespace entry

Plain `nsenter -n -t <pid>` as the invoking user fails with `EPERM`. Both of these work on **all three**
container modes (including `keep-id`):

```
nsenter -U -n -t <pid> -- <cmd>
podman unshare nsenter -n -t <pid> -- <cmd>
```

Both enter with a full capability set in the target userns (`CapEff=000001ffffffffff`). Interface names
confirm design finding 9: pasta containers inherited the host Wi-Fi name `wlp0s20f3`, not `eth0`; the
bridge container used `eth0`. Resolving the interface from the default route inside the netns is
required.

## qdisc writes

`tc qdisc replace dev <iface> root handle 5753: pfifo|netem` and subsequent deletion succeeded through
both entry forms. The bridge veth had `qdisc noqueue` by default, yet 150 ms netem delay took effect —
the `direct_qlen` workaround from moby#33162 was **not** needed on this kernel/runtime.

## Measured delay (bridge container, ping from `podman unshare --rootless-netns`)

| Applied | RTT (avg of 5) |
|---|---|
| baseline | 0.275 ms |
| egress 150 ms | 150.129 ms |
| ingress 150 ms (clsact + matchall + mirred → `wanshape0` IFB) | 150.182 ms |
| both, 150 ms each | 300.181 ms |
| after clear | 0.120 ms |

`tc qdisc replace ... clsact`, `tc filter replace ... ingress pref 49152 ... matchall action mirred
egress redirect dev ...` and cleanup (`filter del`, `qdisc del clsact`, `ip link del`) all worked inside
the rootless netns.

Pasta egress, measured with `ping` from inside the container's netns to the default gateway:
baseline 3.096 ms → 152.505 ms with 150 ms netem → 2.520 ms after clear. Ingress on pasta was not
measured (the container shares the host's address); the mechanism is identical to the validated bridge
case.

## Sidecar backend — design correction

`--network container:<target> --cap-add NET_ADMIN` joins the target netns and grants `cap_net_admin`
(decoded `CapEff=00000000800415fb`). Adding `--userns container:<target>` is **not** always harmless:

- default pasta target, whose userns is the shared rootless userns: `--userns container:<target>`
  fails with `OCI runtime error: crun: setns to target user namespace ...: Invalid argument`
  (the caller is already in that userns — the `setns(2)` `EINVAL` trap from design finding 4);
  omitting `--userns` joins the correct netns/userns and works.
- `keep-id` target, whose userns is a nested child: it needs `--userns container:<target>`; without it
  the sidecar joins the netns but stays in the rootless userns, so `NET_ADMIN` is in the wrong scope.

**Conclusion:** choose `--userns container:<target>` only when the target's user namespace differs from
the Podman rootless userns. Design r4's "always pass it" is wrong; see r5.

## Prototype smoke test (same laptop, 2026-11-25)

A throwaway rootless netavark-bridge container (`wanshape-proto`) was used to exercise the phase-1 CLI:

- `doctor`, `list`, `show 3g` — OK.
- `apply --profile 3g --dry-run` printed six exact `nsenter ... tc/ip` commands and made no changes (`tc qdisc show` stayed `noqueue`, no `wanshape0`).
- `apply --delay 100ms --no-reorder` (both directions) gave ~200 ms added RTT (measured 240 ms avg over 5 pings, with normal rootless-bridge variance); `status` showed handle `5753:`, our filter and `wanshape0` with increasing counters.
- `clear` removed root qdisc, filter, clsact and IFB; ping returned to ~0.14 ms and the state file entry was removed.
- `status` all-targets now resolves by stored container ID and prints the container name.
- The throwaway container, network and temporary state directory were removed.

## Still to verify on WSL2

- Kernel module availability/autoload on Microsoft's WSL kernel (laptop result does not transfer).
- All of the above on WSL2's networking stack, including pasta loopback splicing.
- Rootful Docker and rootful Podman (Docker is not installed on the laptop).
- Egress-loss behaviour for TCP vs UDP, reordering with `pfifo` child, and host port-filter mode.

# WSL2 and live CallRaven test — the PC, 2026-11-25

Environment: Fedora 44 under WSL2 (mirrored networking), kernel `6.6.114.1-microsoft-standard-WSL2`,
iproute2 6.17.0, rootless Podman. FreePBX 17 on a VMware VM on the same Windows host (192.168.1.167).

## Kernel

`sch_netem`, `ifb`, `act_mirred`, `cls_matchall` and `cls_flower` are all available as modules on
Microsoft's kernel and load on first use. No custom kernel needed. Loading `ifb` also creates two idle
devices, `ifb0` and `ifb1` (the module's `numifbs=2` default); they are not wanshape's.

## Test suites

Unit tests, `tests/integration/host_mode.sh` and `tests/integration/netns_mode.sh` (rootless bridge,
pasta, keep-id) all pass on WSL2.

## Host mode against CallRaven

The CallRaven tester (`callraven`, host networking, ext 1114, SIP 5060, RTP 10000-20000) called the
agent CallRaven (ext 1112) through FreePBX; the agent auto-answered with its echo responder on. Only the
tester was shaped, with `sudo ./bin/wanshape apply-host callraven --via 192.168.1.167` (ports read from
its environment; interface `eth0`). G.722 calls; figures from CallRaven's `/api/stats` and
`/api/audiotest` (10 tone probes through the echo).

| Run | RTCP RTT | Probe round trip (avg) | Jitter | CallRaven rx loss | MOS | netem drops up / down |
|---|---|---|---|---|---|---|
| baseline | 1 ms | 4 ms | 6 ms | 0.0% | 4.37 | — |
| `3g` (150 ms ±30 each way, 1%) | 356 ms | 367 ms | 13 ms | 0.2% | 3.91 | 35 / 122 (~1.1%) |
| `lossy-sip` (50 ms ±15, gemodel), re-applied over `3g` | 120 ms | 123 ms | 13 ms | 1.5% | 4.17 | 62 / 261 (~3%) |
| after `clear-host` | 4 ms | 5 ms | 7 ms | 0.0% | 4.37 | — |

Delay and jitter land as configured, re-apply works on the live host, and `clear-host` removed every
filter, both IFBs and the `clsact` it added. All 10 probes came back on every run.

Open: CallRaven's loss figures are lower than what netem dropped (rx 0.2% vs ~1.1%; 1.5% vs ~3%), and
its `remoteLossPercent` stayed 0 although uplink packets were dropped. Its `rxPackets` is also about
3.5x `txPackets` on a G.722 call. These look like questions about how CallRaven counts, worth checking
in CallRaven against a pcap, not wanshape behaviour.

# VM mode spike — laptop, 2026-11-25

Can wanshape shape a libvirt VM the way it shapes a host-network container? There is no passwordless
sudo on the laptop, so the spike recreated libvirt's NAT/bridge layout in a private user + network
namespace: a bridge `br0` with a tap `vnet0` (MAC `fe:` + guest MAC, as libvirt sets it) and a CirrOS
0.6.2 guest under KVM (`qemu-system-x86_64 -netdev tap`). The guest was reached on its IPv6 link-local
address; dnsmasq would not hand out leases inside the user namespace.

| tc on the tap `vnet0` | ping to the guest |
|---|---|
| none | 0.6 ms |
| clsact **egress** matchall -> IFB netem 100 ms | 100.8 ms |
| + **ingress** matchall -> IFB netem 30 ms | 130.9 ms |
| ingress only | 30.8 ms |
| cleared | 0.7 ms |

100% loss on the egress IFB dropped the echo requests: **the tap's egress hook carries traffic to the
guest and its ingress hook traffic from it**, the reverse of a container's interface. `apply-vm` maps
them accordingly (`tests/integration/vm_mode.sh` checks it with 100% loss per direction).

Also found: libvirt's `<bandwidth><outbound>` puts a plain `ingress` qdisc on the tap, and `tc qdisc add
clsact` then fails with "Exclusivity flag on, cannot modify". wanshape detects it and refuses (exit 4).

Not covered yet: a libvirt-managed VM under sudo (resolution was checked against the real
`virsh domiflist` on the laptop, whose FreePBX VM is `direct` and correctly refused); `direct`
(macvtap) interfaces, whose hook directions need measuring as root; VM-to-VM traffic across the bridge
(expected to be shaped, since the bridge sends to the tap through the same egress hook, not measured).

## VM mode on libvirt — the PC, 2026-11-25

Two throwaway CirrOS VMs (`wanshape-test1/2`, 256 MB) on libvirt's `default` NAT network under WSL2,
created with `virt-install`, shaped with `sudo ./bin/wanshape apply-vm` (real `virsh` lookup and tap MAC
check), and removed afterwards with their disks.

| Step | ping host -> VM1 |
|---|---|
| baseline | 0.47 ms |
| `--delay 40ms` | 80.5 ms |
| `3g` | 284 ms |
| VM1 destroyed and started: tap `vnet0` -> `vnet2`; `status` reports STALE; re-applied `--delay 25ms` | 50.5 ms |
| `clear-vm` | 0.36 ms |

100% loss per direction dropped the host's pings on the expected IFB (`--direction egress` on
`wsh0up`, `ingress` on `wsh0dn`). `virsh domiftune --outbound 1000` on VM2 added an `ingress` qdisc to
its tap; `apply-vm` then refused with exit 4 as designed. Nothing was left on the taps or as IFBs.

Not measured: VM-to-VM traffic across the bridge. CirrOS's SSH server never came up on VM2, and a
login over its serial console got no answer; tracked as an issue.

# Loss on the way out: TCP and UDP — laptop, 2026-11-25

Design §2.6 worried that netem loss applied to a host's own outgoing traffic is reported back to the
sending socket, so TCP would resend at once and barely notice. Measured with iperf3 across a veth pair
in a private namespace (kernel 7.3), 4 mbit/s cap, 6 s per test, two runs:

| 3% loss applied | TCP received | TCP retransmits | UDP lost (1 mbit/s, ~520 packets) |
|---|---|---|---|
| nowhere | 3.75-3.80 mbit/s | 0 | 0.0% |
| on the sender's way out (wanshape's "up") | 3.65-3.74 mbit/s | 45-108 | 2.1-2.5% |
| on the receiver's way in (IFB at the receiver) | 3.64-3.65 mbit/s | 49-57 | 3.3-4.4% |

TCP retransmits as much with the loss on the sender's side as at the receiver, and throughput is the
same, so the drop is not masked: netem on current kernels doesn't report it to the socket. UDP loses
about the configured share either way (the runs are short, so the figures are noisy). No caveat is
needed in `show` or `status`.

# netem rate vs HTB ("--rate-mode accurate") — laptop, 2026-11-25

`apply-host` in a private namespace, outgoing only, 10 ms delay and a 4 mbit/s cap; iperf3 TCP for 6 s,
then UDP offered at 8 mbit/s for 6 s. Two runs each, the same both times:

| rate mode | TCP received | UDP dropped | UDP jitter |
|---|---|---|---|
| netem's own `rate` | 3.77 mbit/s | 27.4% | 0.18-0.20 ms |
| HTB class + netem below it | 3.76 mbit/s | 27.3% | 0.20 ms |

No measurable difference on this kernel; `accurate` stays opt-in. (UDP loss is below 50% because the
queued packets still arrive after the 6 s send.)
