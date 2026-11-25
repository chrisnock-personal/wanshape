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
