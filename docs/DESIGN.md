# wanshape — design proposal

**Status:** draft, 2026-11-25 (revision 7: MVP with host port-filter mode; see §10.1, §14 and `README.md`). Working name; rename is cheap before phase 1.
**Scope of this document:** design and implementation notes. The Python prototype currently covers the phase-1 core described in §12; see `README.md` for what is and is not implemented.

A CLI that applies netem/tc WAN impairments (latency, jitter, loss, bandwidth) to **one named container**, on both Podman and Docker, without requiring the container image to carry `tc` or the container to be started with special capabilities.

## 1. Goals and non-goals

### Goals

- `wanshape apply <container> --profile 3g` — one command, both directions by default.
- Works on: rootful Docker bridge, rootful Podman bridge, rootless Podman bridge (netavark), rootless Podman default (pasta / slirp4netns).
- Egress **and** ingress shaping, latency + jitter + loss + bandwidth cap.
- Built-in and user-defined named profiles, including bursty (Gilbert-Elliott) loss for VoIP testing.
- Dry-run that prints the exact `tc` commands; explicit, targeted `clear`.
- No daemon, no root requirement when the target is a rootless container owned by the invoking user.

### Non-goals for v1

- Host-network containers as a per-container target (see §4 and §10 — a hard limitation, not a missing flag). §10 describes the port-filter mode that covers the main host-network use case; whether that lands in v1 is a decision to make before phase 1 (§13).
- Per-flow/per-port filtering on bridge/pasta targets. Whole-container interface only.
- Rootless Docker (RootlessKit). Likely works the same way as rootless Podman, but untested and not in the matrix.
- macOS/Windows, Docker Desktop, Kubernetes.
- Surviving a container restart or recreation. Qdiscs live in the kernel netns; when the netns goes away, so does the shaping. The tool detects this and marks state as stale.
- Persisting profiles through a daemon or HTTP API.

## 2. Findings that shape the design

These are the constraints that make this more than a wrapper around `tc qdisc add`:

1. **`netem` is egress-only.** For container *ingress* (download), traffic must be redirected via a `clsact` ingress hook + `mirred` to an IFB device, and the impairments applied to the IFB's egress. The IFB module must be loaded on the host (`modprobe ifb`) — the tool can check and report, but rootless cannot load it itself.
2. **Rootless Podman's default network is pasta** (Podman 5+; older versions use slirp4netns). There is no host-side veth for the tool to shape: the host end is a userspace process. All shaping must happen **inside the container's network namespace**.
3. **Rootless Podman bridge uses an extra network namespace** (`podman unshare --rootless-netns`); the host-side veth is not visible from the root netns. Again, in-netns shaping is the portable answer.
4. **`CAP_NET_ADMIN` is not in the default capability set** for Docker or Podman containers. We cannot assume we can `podman exec tc` inside the target. The portable route is to enter the target's netns from the outside with `CAP_NET_ADMIN` in the user namespace that owns it, running the **host's** `tc` binary (the mount namespace is not entered, so the image needs no `tc`). Two candidate invocations, both to be tried in the phase-0 spike:
   - `nsenter -U -n -t <pid> -- tc ...` run directly as the owning user. Per `user_namespaces(7)`, a process in the parent userns whose effective UID owns a child userns has all capabilities in it, and the kernel (`cap_capable()`) applies that rule down the whole tree. The rootless userns is a child of the initial userns owned by the invoking user, so the user holds `CAP_SYS_ADMIN` in it and in any userns nested under it (`keep-id`, `auto`). Expected to work without `podman unshare`; this is the form the gabriel.urdhr write-up uses.
   - `podman unshare nsenter -n -t <pid> -- tc ...` (already uid 0 in the rootless userns; enter the netns only). Expected to work for the default case, where the container lives in the rootless userns itself.

   Neither form is ranked ahead of the other until the spike has run both. One trap: `podman unshare nsenter -U -n` fails with `EINVAL` when the target *is* the rootless userns (`setns()` into the caller's own userns), but works for a `keep-id`/`auto` container, whose userns is a child of it. The tool must not depend on that difference by accident. The spike must also cover `--userns=keep-id` and `--userns=auto` containers, whose netns may be owned by a different userns. If in-netns entry fails, the fallback is the sidecar backend (finding 10), not `--cap-add=NET_ADMIN` on the target.
5. **netem can do rate limiting itself** (`rate <rate> [overhead [cellsize [celloverhead]]]`), so a single qdisc covers delay/jitter/loss/rate. HTB/TBF children are only needed if we later want precise rate control or classful per-flow shaping. Caveat from `tc-netem(8)`: rate throttling is affected by kernel timer granularity and can burst; for TCP realism the manpage recommends placing netem at the *receiver's* ingress. We document this rather than pretend the numbers are exact.
6. **Loss on egress may not look like loss.** The Linux Foundation netem docs (not the current `tc-netem(8)`) warn that loss applied locally is reported to upper-layer protocols, so TCP may resend immediately and behave as if nothing was lost. Inside the container's netns, everything leaving is locally generated. Consequences: the "down" direction (IFB = receiver side) is realistic; "up" loss is applied on egress with a documented caveat, and the spike measures how TCP and UDP actually behave with it. If it turns out to be meaningless for TCP, `status`/`show` say so rather than reporting a loss figure that isn't happening.
7. **netem with jitter reorders packets.** Its internal `tfifo` queue releases packets by send time, so jitter larger than the inter-packet gap reorders them. For RTP that is a real choice, not a detail: reordering exercises the jitter buffer differently from pure delay variation. Profiles state it explicitly (`reorder_on_jitter = true|false`); `false` attaches a `pfifo` child under netem, which keeps order.
8. **Virtual interfaces may have no qdisc attached** (`noqueue`); delay may silently do nothing. Known workaround (moby#33162): set `direct_qlen 1000` or raise `ip link set dev eth0 qlen 1000`. Include in the planner.
9. **Pasta specifics.**
   - Pasta copies the host's interface name into the namespace (on WSL that is `eth0`, elsewhere `enp3s0`, `wlp2s0`, …). The interface is therefore resolved from the **default route inside the netns**, never assumed to be `eth0`.
   - Pasta *splices* loopback connections: a host connection to a published port on `127.0.0.1` arrives on the container's `lo`, not the tap device, and is **not shaped**. Tests and users must reach the container via a non-loopback address. Documented prominently and covered in §11.
10. **Sidecar backend.** `podman run --rm --network container:<target> [--userns container:<target>] --cap-add NET_ADMIN <tc-image> tc ...` (Docker: `--network container:` + `--cap-add NET_ADMIN` only). `CAP_NET_ADMIN` is scoped to the userns that *owns* the netns. In the default rootless case every container lives in the same rootless userns, so joining the netns is enough; a `keep-id`/`auto` target has its own nested userns, and then the sidecar must join it too (Podman #19185 arrived at exactly this combination). **`--userns` must be conditional**, not always-on: passing it when the target's userns is the rootless userns the caller is already in fails with `EINVAL` (`SPIKE-RESULTS.md`). Compare the target's userns with the Podman rootless userns and add the flag only when they differ. It works rootful and rootless, and for docker-group users who are not root — Pumba does exactly this on Docker. Cost: a small image with `tc` must be present (build locally; no network pull assumed).
11. `nsenter -t <pid>` is a TOCTOU risk if the container main process dies during resolution. Resolve PID → netns inode early, re-verify the inode before applying, and fail rather than shaping a recycled PID.
12. **Kernel support on WSL2 is not a given.** Everything here runs in Fedora WSL2 on Microsoft's kernel. It must have `sch_netem`, `ifb`, `act_mirred`, `cls_matchall` (and `cls_u32` for §10) built in or as loadable modules. Checked first in phase 0 and by `doctor`; if missing, the alternative is a custom WSL kernel (`.wslconfig` `kernel=`), which is a much bigger ask.

### 2.1 Prior art — why not an existing tool

| Tool | What it does | Why it doesn't fit as-is |
|---|---|---|
| Pumba (`pumba netem`) | Sidecar container with `tc` in the target's netns | Docker API only (Podman's Docker-compatible socket might work — check in phase 0); no ingress shaping; no Podman rootless story |
| `tcconfig` (`tcset --docker`) | Shapes the host-side veth | Root and Docker only; host-side veth doesn't exist for pasta or rootless bridge |
| comcast | Host-level tc/iptables wrapper | Whole host, not per container; unmaintained |
| Toxiproxy | TCP proxy that injects latency/faults | No UDP, so useless for SIP/RTP |

If phase 0 shows Pumba works against Podman's socket for the cases we care about, reconsider building this at all.

## 3. Architecture

```
wanshape CLI
  ├── profile loader       built-in TOML + ~/.config/wanshape/profiles.toml
  ├── target resolver      runtime detection → Target { netns, iface, caps, owner, netns_inode }
  ├── command planner      Profile + direction → ordered list of ip/tc commands
  ├── exec backend         root nsenter | rootless userns nsenter | sidecar container
  ├── live inspector       reads tc/ip state in the netns; source of truth for status/clear
  ├── state store          ~/.local/state/wanshape/state.json (XDG): profile name + timer only
  └── reporter             dry-run, status, tc -s stats, doctor
```

The **planner emits commands, not side effects**. Every command goes through the same executor, so `--dry-run` is mechanically the same path as a real apply and cannot drift.

### Target record

```
Target {
  name/id, runtime (podman|docker), rootless: bool,
  pid, netns_path, netns_inode, user_ns_path,
  network_mode (bridge|pasta|slirp4netns|host|container:|pod|unknown),
  iface (from the default route in the netns; overridable), host_veth (if any),
  owner_uid, capabilities
}
```

Resolution sources: `podman inspect`/`docker inspect` for PID, `HostConfig.NetworkMode`, `NetworkSettings`, `/proc/<pid>/ns/{net,user}`, and `ip route show default` inside the netns for the interface. Never parse `ps`.

## 4. Runtime / network-mode matrix

| Case | Where the qdisc goes | Invocation sketch | Notes |
|---|---|---|---|
| Docker rootful, bridge | inside container netns | `nsenter -t $PID -n -- tc ...` | Host root. Sidecar backend if the caller is in the docker group but not root. |
| Podman rootful, bridge | inside container netns | same | Same as Docker. |
| Podman rootless, bridge (netavark) | inside container netns | `nsenter -U -n -t $PID -- tc ...` or `podman unshare nsenter -n -t $PID -- tc ...` | Netns is owned by the Podman userns. Spike decides which form. |
| Podman rootless, pasta/slirp4netns | inside container netns | same | No host-side veth exists — this is the only option. Interface name copied from the host; loopback splicing bypasses shaping (§2.9). |
| `--network=container:X` / pod | shared netns of X / infra container | resolve to the owning netns inode | Warn: shaping affects every container sharing that netns. Dedupe by inode. |
| `--network=host` | — | **refuse** per-container shaping | No per-container netns or interface. See §10 for the port-filter mode. |
| Docker rootless | — | not supported in v1 | Listed so it fails with a clear message rather than by accident. |

Host-side veth shaping is deliberately **not** the primary mechanism. It inverts direction (qdisc on host veth egress = traffic *into* the container), only exists for bridge modes, and needs IFB for the other direction. It may be added later as an optimization or fallback for rootful bridge, but it should not be the core abstraction.

## 5. Shaping model

Let `IFACE` be the container's primary interface (resolved from the default route; override with `--iface`). All wanshape qdiscs use the fixed handle **`5753:`** ("WS"), so the live inspector can recognise them without the state file.

### Egress (container → network = "upload")

```
tc qdisc replace dev $IFACE root handle 5753: netem \
    delay 150ms 30ms distribution normal \
    loss 1% \
    rate 768kbit \
    limit 1000
# only when reorder_on_jitter = false:
tc qdisc replace dev $IFACE parent 5753:1 pfifo limit 1000
```

- `replace`, not `add`, for idempotency.
- Before replacing, inspect the existing root qdisc. If it is anything other than the default `noqueue`/`fq_codel` or one with handle `5753:`, refuse unless `--force` (we cannot reliably reconstruct arbitrary qdisc trees from `tc qdisc show`).
- `--rate-mode accurate` (later) would use HTB + child netem; v1 uses netem's built-in `rate`.
- Loss here has the local-traffic caveat of §2.6.

### Ingress (network → container = "download")

```
ip link add wanshape0 type ifb
ip link set wanshape0 up
tc qdisc replace dev $IFACE clsact
tc filter replace dev $IFACE ingress pref 49152 protocol all \
    matchall action mirred egress redirect dev wanshape0
tc qdisc replace dev wanshape0 root handle 5753: netem <down options>
```

- `clsact` + `matchall` rather than `ingress` + `u32 match u32 0 0`: simpler, and `replace` keeps it idempotent. A fixed filter `pref` lets `clear` delete exactly our filter. **Guard before `replace`:** inspect the interface first and refuse if a foreign `clsact` qdisc or filter is present — `replace` would silently delete another tool's filters. Same rule as for the root qdisc (below, `--force`).
- The IFB is created **inside the container's netns**, so names cannot clash between containers; a fixed name `wanshape0` is enough. It disappears with the netns. Cleanup removes the filter, the `clsact` qdisc (only if wanshape added it) and the IFB.
- Verify the `ifb` module is available up front (`doctor`), because in a rootless userns the tool cannot `modprobe`.
- `--direction egress|ingress|both`, default `both`. If IFB setup fails, apply egress anyway and report partial success with the exact failure (rather than silently doing half of `both` without saying so).

### Direction vocabulary

Document prominently: **egress = upload from the container's point of view; ingress = download**. Profile fields are named `up`/`down` for humans and mapped to egress/ingress internally. All delays are **one-way, per direction**: a profile with 150ms each way adds ~300ms to RTT.

## 6. CLI

```
wanshape doctor                                  # preflight: kernel modules, tc, nsenter, ifb, runtimes, caps
wanshape list                                    # built-in + user profiles
wanshape show <profile>                           # expand a profile into effective settings
wanshape apply <container> [--runtime podman|docker] [--iface IFACE]
       (--profile NAME | --delay T [--jitter T] [--loss PCT | --loss-model gemodel:P,R[,1-H[,1-K]]] [--rate RATE])
       [--no-reorder] [--direction egress|ingress|both] [--duration DURATION]
       [--backend auto|nsenter|sidecar] [--dry-run] [--force]
wanshape status [<container>]                    # applied profiles + tc -s counters, read from the live netns
wanshape clear <container> [--direction ...] [--dry-run]
```

Examples:

```
wanshape apply callraven-agent --profile bad-wifi --dry-run
wanshape apply callraven-agent --profile satellite --direction ingress
wanshape apply sip-test --delay 100ms --jitter 20ms --loss 3% --rate 512kbit
wanshape status
wanshape clear callraven-agent
```

Exit codes: 0 success, 1 generic error, 2 unsupported target (e.g. host network), 3 prerequisite missing, 4 state conflict (already shaped with a different profile, no `--force`).

`--duration` for v1 is implemented by scheduling a one-shot `systemd-run --user --on-active=<duration> wanshape clear <container>` (or `at` if `systemd-run` is unavailable), with the job ID recorded in state. This is a convenience, not a guarantee — document that it dies with the user session. A real daemon is explicitly out of scope.

## 7. Profiles

TOML, because Python's `tomllib` reads it with no dependency. Built-ins ship in the package; `~/.config/wanshape/profiles.toml` can override a built-in name or add new ones.

```toml
[profiles.3g]
description = "3G / poor mobile"
reorder_on_jitter = true
up   = { delay = "150ms", jitter = "30ms", distribution = "normal", loss = "1%", rate = "768kbit" }
down = { delay = "150ms", jitter = "30ms", distribution = "normal", loss = "1%", rate = "1600kbit" }

[profiles.lossy-sip]
description = "VoIP over a congested link: bursty loss"
reorder_on_jitter = false
up   = { delay = "50ms", jitter = "15ms", distribution = "normal", loss_model = "gemodel 1% 25% 100% 0%", rate = "512kbit" }
down = { delay = "50ms", jitter = "15ms", distribution = "normal", loss_model = "gemodel 1% 25% 100% 0%", rate = "512kbit" }
```

`loss` (independent random loss) and `loss_model` (netem `gemodel`/`state`, i.e. bursty loss) are mutually exclusive. Bursty loss is the one that matters for codec packet-loss concealment and jitter buffers, so it is in v1, not behind `--raw-netem`.

Built-ins (starting values, to be tuned during testing). **Delay is one-way, applied in each direction**; the RTT column is what `ping` should show on top of the baseline.

| Profile | One-way delay | Added RTT | Jitter / dist | Loss | Reorder | Down / Up |
|---|---|---|---|---|---|---|
| `office` | 5ms | ~10ms | 1ms / normal | 0% | no | 100mbit / 100mbit |
| `4g` | 25ms | ~50ms | 10ms / normal | 0.1% | no | 10mbit / 5mbit |
| `3g` | 150ms | ~300ms | 30ms / normal | 1% | yes | 1600kbit / 768kbit |
| `satellite` (GEO) | 300ms | ~600ms | 20ms / normal | 0.5% | no | 5mbit / 1mbit |
| `bad-wifi` | 40ms | ~80ms | 60ms / pareto | 5% random | yes | 2mbit / 1mbit |
| `lossy-sip` | 50ms | ~100ms | 15ms / normal | gemodel (bursty, ~1–4% avg) | no | 512kbit / 512kbit |

Extra netem options (duplicate, corrupt, gap, ecn, seed, correlation, slot) map 1:1 from profile fields to CLI flags; v1 exposes delay, jitter, distribution, loss/loss model, rate and reordering, with `--raw-netem` as an escape hatch for the rest.

## 8. State and lifecycle

**The kernel is the source of truth.** `status` and `clear` inspect the live netns for qdiscs with handle `5753:`, the wanshape filter `pref` and the `wanshape0` IFB. That way a lost, out-of-date or other-user state file (rootful targets run as root, so their state lands in `/root/.local/state`) can never strand shaping or hide it.

The state file `~/.local/state/wanshape/state.json` holds only what the kernel can't tell us, keyed by container ID:

```
{ "id": "...", "name": "callraven-agent", "netns_inode": 4026532500,
  "profile": "3g", "applied_at": "...",
  "prior_root_qdisc": "noqueue", "added_clsact": true,
  "timer": "systemd-run job id or null" }
```

- `apply` is idempotent for the same profile+netns; a different profile replaces cleanly.
- `status` compares stored `netns_inode` with the live one; a mismatch means the container was recreated → report "stale" and offer to prune. Never `clear` against a recycled PID.
- `status` without a state entry still reports wanshape qdiscs it finds live (profile shown as "unknown").
- `clear` restores the previously observed root qdisc where possible (`noqueue` → `tc qdisc del dev $IFACE root`), removes the IFB and `clsact` only if wanshape created them, and never touches interfaces or qdiscs that aren't wanshape's.
- Multiple targets are independent; a shared netns is detected by inode and reported as one shaping domain.

## 9. Safety and validation

- Resolve container → PID → netns inode → re-verify inode before every mutation.
- Refuse ambiguous names (short IDs that match >1 container) and non-running containers.
- Refuse host-network per-container shaping with an actionable message: "host networking has no per-container netns; use `wanshape apply-host --ports ...` (§10) or attach the container to a bridge network".
- Refuse to overwrite a non-default qdisc unless `--force`; print what was there.
- Refuse to overwrite an existing `clsact` qdisc or filter that isn't wanshape's, unless `--force` (§5); never silently delete another tool's filters.
- Every mutation is printed (also in dry-run) with the exact command and the namespace entry prefix.
- Validate units/ranges before generating commands (delay ≥ 0, jitter ≤ delay for normal distribution sanity, 0 ≤ loss ≤ 100, gemodel probabilities in range, parse rates with `tc`'s own syntax rules).
- No `sudo` escalation by default: for rootless targets, run as the owner; for rootful targets, require the caller to already be root (or use the sidecar backend, or fail with a clear message rather than prompting).

## 10. Host-network containers — the known gap

CallRaven and CallRaven-agent run with host networking, so **per-container shaping cannot reach them**, and they are the main reason this tool exists. Resolve this before phase 1. Options, in order of preference:

1. **Port-filter mode on the host interface (`wanshape apply-host`).** Their traffic is identifiable by port (values from `podman inspect` on the laptop travel stack, 2026-11-25; confirm on the PC in phase 0):

   | Container | SIP | RTP |
   |---|---|---|
   | `callraven` (tester, ext 1114) | 5060 | 10000–20000, plus 30000–30999 for load-test media (`LOAD_RTP_PORT_LOW..HIGH`) |
   | `callraven-agent` (ext 1112) | 5062 | 21000–21999 |

 A `prio` (or `htb`) root on the host interface with `u32` filters on those source/destination ports sends only matching traffic to a netem band; an IFB with the same filters covers the download direction. Needs root in WSL (fine there) and touches only the named ports. Far less invasive than option 4. If adopted, it lands in v1 and per-port filtering stops being a non-goal for this mode only.

   The ranges don't overlap, so ports alone tell the two containers apart. The caveat runs the other way: this mode is host-interface shaping keyed on ports, so `apply-host` must be given **every** range a container uses (for the tester, SIP 5060 *and* 10000–20000 *and* 30000–30999), and a container whose ranges change in its `.env` silently drops out of shaping. `apply-host` should therefore accept a container name and read `SIP_PORT`, `RTP_PORT_LOW/HIGH` and `LOAD_RTP_PORT_LOW/HIGH` from its inspected environment, with `--ports` as an explicit override, and refuse if two targets' ranges overlap.
2. **Shape the other end.** Running `tc` inside the FreePBX VM (on its interface towards the CallRaven host) affects every call to and from the PBX without touching the containers. Zero code; good for one-off tests. Not a substitute for a tool, because it shapes everything the PBX does.
3. **Move them to a bridge network.** Then this tool works unchanged. Not as cheap as it looks: on a bridge or pasta network SIP is behind NAT, so Contact/SDP addresses must advertise the external IP and the RTP range must be forwarded. Worth doing only if CallRaven needs that anyway.
4. **Later, opt-in advanced mode:** tag the container's cgroup (cgroup skb eBPF or `iptables -m cgroup -j MARK`) and match the mark with `tc filter ... fw` on the host interface. Egress only is straightforward; ingress cannot be attributed to the container because it arrives as host-bound traffic. Significantly more invasive and fragile.
5. **Not planned:** shaping the whole host interface, which affects every other container and the host itself.

This limitation should be on the README's first screen, not buried in troubleshooting.

### 10.1 As built (r7): host mode

Option 1 is implemented as `wanshape apply-host` / `clear-host`, with three changes from the sketch above:

- **No `prio` root on the host interface.** A `clsact` qdisc carries `flower` filters on both hooks; matching packets are redirected with `mirred` to a per-target IFB pair (`wsh<N>up` for egress, `wsh<N>dn` for ingress) whose root is the netem qdisc. Egress redirect to an IFB returns the packet to the original device after netem. The host's root qdisc and all non-matching traffic are untouched, and clearing a target deletes only its filters and IFBs.
- **`flower` instead of `u32`.** `flower` matches port ranges directly (`src_port 10000-20000`), where `u32` would need each range split into value/mask prefixes. Four filters per range: IPv4/IPv6 x UDP/TCP.
- **Local port only.** Egress matches `src_port`, ingress `dst_port`, so a remote peer that happens to use the same port numbers (FreePBX's RTP range is also 10000-20000) is not matched by accident.

Each target takes a slot N in 0-9: filter prefs `49300 + 100*N` upwards on both hooks, and its metadata (name, profile, interface, ports, direction, clsact ownership) in the alias of `wsh<N>up`. The kernel is the whole state for host mode, readable without root, with no state file. `clsact` ownership is shared between wanshape targets on an interface, so whichever target is cleared last removes it, unless foreign filters remain. Ports come from `--ports`, else the container's `wanshape.ports` label, else its `*SIP_PORT` and `*PORT_LOW/HIGH` environment variables; overlapping targets on one interface are refused. Under sudo, rootless containers are inspected as `SUDO_USER`. On failure the apply is rolled back. Verified in `tests/integration/host_mode.sh` (veth pair in a private user+net namespace: UDP and TCP, ranges and single ports, re-apply, overlap refusal, two targets, ordered clears, nothing left behind).

## 11. Verification plan

Test matrix — one container per row, apply `3g` with `--direction both`, then measure:

| Runtime / mode | RTT (ping -c 50 in container) | Loss | Rate (iperf3) | Ingress path |
|---|---|---|---|---|
| Docker rootful bridge | baseline vs +~300ms RTT (150ms each way) | ~1% | ~768kbit up / ~1600kbit down | iperf3 `-R` |
| Podman rootful bridge | same | same | same | same |
| Podman rootless bridge | same | same | same | same |
| Podman rootless pasta | same; note pasta userspace overhead | same | same | same, via a **non-loopback** address |
| Sidecar backend (one rootful, one rootless) | same | same | same | same |

For each row: `tc -s qdisc show` counters must increase, `clear` must return to baseline, and `status` must show no stale entry. Also test:

- Container recreated while shaped → `status` says stale.
- `--dry-run` makes no changes.
- Host-network target → clean refusal.
- IFB unavailable → egress applied with explicit partial-success warning.
- Pasta: a connection via `127.0.0.1` is **not** shaped (documents the splice), one via the LAN address is.
- Egress loss: iperf3 TCP and UDP with `up` loss only; record whether TCP sees retransmits/throughput drop or masks the loss (§2.6).
- Reordering: UDP sequence test with `reorder_on_jitter` true vs false.
- State file deleted while shaped → `status` still finds it, `clear` still removes it.
- `--userns=keep-id` rootless container.
- If §10 option 1 is adopted: `apply-host` on a SIP call through FreePBX shows the impairment on the call (RTP jitter/loss in CallRaven) and none on unrelated host traffic.
- `apply-host callraven-agent` shapes the agent's calls and leaves a simultaneous tester call unaffected (and the reverse); a tester load test (30000–30999 media) is shaped when the tester is the target.

Remember `ping` may need `NET_RAW` or a permissive `net.ipv4.ping_group_range`; use `curl`/TCP timing instead if needed.

## 12. Implementation phases

- **Phase 0 — spike (blocker).** Laptop run is done (`SPIKE-RESULTS.md`); the WSL2 run is still pending. In this order:
  1. WSL2 kernel: `sch_netem`, `ifb`, `act_mirred`, `cls_matchall`, `cls_u32` present (built in or modules). If not, stop and decide about a custom kernel.
  2. Prior art: does Pumba work against Podman's Docker-compatible socket for rootful and rootless? If it covers the cases, reconsider the project.
  3. Rootless netns entry: both `nsenter -U -n -t` and `podman unshare nsenter -n -t`, for pasta and bridge, plus a `keep-id` container.
  4. IFB creation inside a rootless netns; `direct_qlen` workaround; egress-loss behaviour (§2.6).
  5. Sidecar backend with a locally built `tc` image, rootful and rootless, with `--userns container:<target>` only when the target's userns differs from the caller's rootless userns.
  6. Decide the host-network answer (§10). Confirm the PC's CallRaven port ranges match the laptop's (§10 table).

  No CLI code until this passes.
- **Phase 1 — core.** Repo skeleton, CLI, target resolver, command planner/executor, live inspector, profiles, egress/ingress netem, dry-run, clear, status. A Python prototype exists (`README.md`); Podman rootless bridge is smoke-tested, Docker/rootful and WSL2 are not.
- **Phase 2 — rootless + ingress.** Rootless backends, sidecar backend, IFB, direction handling, partial-success reporting.
- **Phase 3 — profiles + lifecycle.** Built-ins/user TOML (including loss models and reordering), state file, `--duration`, `doctor`, README, tests. `apply-host` here if §10 option 1 was chosen.
- **Phase 4 — optional.** Shared-netns/pod handling, host-net cgroup mode, per-flow filters on bridge targets, `rate-mode accurate`, rootless Docker.

## 13. Open questions

- ~~Host-network answer (§10)~~ Port-filter mode, built in r7 (§10.1).
- ~~Ports from the environment or `--ports`?~~ Both: `--ports` > `wanshape.ports` label > environment.
- Final name (`wanshape` vs something in the `call*`/`*nest` family).
- Python vs Go for the CLI: Python keeps the zero-dependency TOML story and matches the repo style; Go gives a single static binary that could be dropped into containers. The sidecar backend removes most of the need for that, so: Python.
- Should `--duration` be dropped from v1 if `systemd-run --user` is not reliable in WSL? (WSL user sessions are unusual; verify during phase 3.)
- ~~Overview page in `project-overviews/` at project start or once phase 1 works?~~ Once phase 1 works.

## 14. Revision history

- **r7, 2026-11-25** — MVP: `apply-host`/`clear-host` (§10.1: clsact + flower + IFB pair per target, kernel-only state in the IFB alias). Fixed: gemodel arguments passed to `tc` as one string; `--loss-model` CLI syntax; re-apply failing on the existing IFB and on the matchall filter (`EEXIST`, not replaceable even with a handle); re-apply losing the prior root qdisc and clsact ownership; leftover `pfifo` child when reordering is re-enabled; `1s` time values; exit codes now typed (2 unsupported, 3 prerequisite, 4 conflict). Integration tests for both modes; `netns_mode.sh` covers rootless bridge, pasta and `keep-id` with measured RTTs.
- **r6, 2026-11-25** — phase-1 prototype scaffold: Python CLI, target resolver, nsenter backend, profiles, live inspector, planner/executor, state file, README and unit tests. Smoke-tested on a rootless Podman bridge container (dry-run, apply, status, clear and baseline restore). Sidecar backend, `apply-host`, `--duration` and WSL2 validation remain unimplemented.
- **r5, 2026-11-25** — laptop phase-0 spike (`SPIKE-RESULTS.md`): both rootless netns-entry forms work on pasta, bridge and `keep-id`; `netem`/IFB/mirred/matchall autoload on demand; egress/ingress/both delay measured at 150/150/300 ms; `direct_qlen` workaround not needed here. Corrected r4: sidecar `--userns container:<target>` must be conditional — passing it for a target already in the caller's rootless userns fails with `EINVAL`.
- **r4, 2026-11-25** — corrections to r3: the two CallRavens do **not** share an RTP range (tester SIP 5060 / RTP 10000–20000 + load 30000–30999; agent SIP 5062 / RTP 21000–21999), so ports distinguish them; `apply-host` reads a container's ports from its environment instead. `nsenter -U -n` as the owning user is expected to work (owner of a userns holds all capabilities in it and its descendants), so the two rootless entry forms are no longer ranked; `keep-id`/`auto` userns are children of the rootless userns, not siblings. Sidecar `--userns container:` is only strictly needed for targets with their own userns, but stays always-on.
- **r3, 2026-11-25** — review pass: sidecar backend must join the target's user namespace on rootless Podman (`--userns container:<target>`); `nsenter -U -n` as the owning user is likely `EPERM`, so the `podman unshare` variant leads; added a guard against clobbering foreign `clsact` filters; noted that the shared RTP range means host port filtering cannot distinguish the two CallRaven containers; corrected the source of the egress-loss caveat.
- **r2, 2026-11-25** — review findings: corrected the rootless nsenter invocation (no `-U` under `podman unshare`); pasta interface naming and loopback splicing; egress-loss caveat; jitter reordering made an explicit profile choice; bursty loss models in v1; sidecar backend; prior-art table; WSL2 kernel check as the first spike item; kernel (fixed handle `5753:`) as source of truth instead of the state file; `clsact` + `matchall` and a fixed in-netns IFB name; fixed the §5 egress rate and the one-way delays (satellite 600ms → 300ms); host-network section reordered around port filtering and the SIP-behind-NAT cost of moving to a bridge; rootless Docker listed as unsupported.
- **r1, 2026-11-25** — first draft.

## References

- `tc-netem(8)`: <https://man7.org/linux/man-pages/man8/tc-netem.8.html> — options, rate caveat, "netem must be placed on ingress of receiver" note, reordering with jitter, `gemodel`/`state` loss models.
- Linux Foundation netem page: <https://wiki.linuxfoundation.org/networking/netem> — "Caveats: when loss is used locally (not on a bridge or router), the loss is reported to upper-level protocols"; this is not in the current `tc-netem(8)`.
- Podman networking tutorial: <https://github.com/containers/podman/blob/main/docs/tutorials/basic_networking.md> — rootless bridge extra netns, `podman unshare --rootless-netns`, pasta default.
- `podman-unshare(1)`: <https://docs.podman.io/en/latest/markdown/podman-unshare.1.html> — invoking user's UID appears as UID 0.
- `user_namespaces(7)`: <https://man7.org/linux/man-pages/man7/user_namespaces.7.html> — a process in the parent userns whose effective UID owns a child userns has all capabilities in it.
- `setns(2)`: joining the caller's own user namespace returns `EINVAL`; joining a user namespace requires `CAP_SYS_ADMIN` in that namespace (`EPERM` otherwise).
- Namespace entry with `nsenter -U -n`: <https://www.gabriel.urdhr.fr/2022/12/03/entering-podman-namespaces/> (includes PID-recycling warning).
- `pasta(1)`: <https://passt.top/builds/latest/web/passt.1.html> — interface name copied from the host, spliced loopback connections.
- Default capabilities: <https://www.redhat.com/en/blog/privileged-flag-container-engines> — `NET_ADMIN` absent by default; rootless caps are userns-scoped.
- IFB / ingress shaping: <https://hechao.li/posts/container-bandwidth-limit>, Ubuntu Server tc/CAKE guide.
- veth qdisc/qlen gotcha: <https://github.com/moby/moby/issues/33162>.
- Pumba: <https://github.com/alexei-led/pumba> — sidecar-container netem approach.
- Podman #19185: <https://github.com/containers/podman/issues/19185> — a sidecar sharing a netns needs `--userns container:<target>` for `NET_ADMIN` to apply in that netns.
