# wanshape — design

**Status:** current as of v0.3.0+ (2026-11-25, revision 9). This describes the tool as built. Measurements behind the decisions are in [`SPIKE-RESULTS.md`](SPIKE-RESULTS.md); profile values and their sources in [`PROFILES.md`](PROFILES.md); usage in the [README](../README.md).

wanshape applies netem/tc WAN impairments (delay, jitter, loss, duplication, corruption, reordering, slotting, a bandwidth cap) to **one target at a time without touching anything else on the machine**: a container with its own network, the ports of a host-network container (or any local ports), or a libvirt VM. It needs no changes to the target and no extra capabilities in it.

## 1. Goals and non-goals

**Goals**
- One command per target (`apply`, `apply-host`, `apply-vm`), both directions by default, profiles or inline settings.
- Rootless Podman without root; rootful targets, host ports and VMs with root.
- Nothing outside the target is shaped, queued differently or reconfigured.
- Exact commands visible before they run (`--dry-run`); clean removal; refusal to overwrite other tools' tc state.
- The kernel as the source of truth.

**Non-goals**
- macOS, Windows, Docker Desktop, Kubernetes.
- Shaping that survives a target being recreated (a new network namespace or tap device); `status` reports it stale. Re-applying after a VM restart is issue #3.
- A daemon or API. Timers (`--duration`) use systemd, not a wanshape process.

## 2. Constraints that shape the design

1. **netem only shapes egress.** Incoming traffic is redirected (`clsact` ingress hook + `mirred`) to an IFB device whose egress carries the netem qdisc.
2. **Rootless Podman has no host-side device to shape** (pasta/slirp4netns are userspace; the rootless bridge lives in an extra namespace). Container mode therefore works *inside* the container's network namespace.
3. **Containers don't have `CAP_NET_ADMIN`.** wanshape enters the target's namespaces from outside and runs the host's `tc`: `nsenter -U -n -t <pid>` as the owner of a rootless container (the owner of a user namespace has every capability in it and its children), `nsenter -n -t <pid>` as root for rootful ones. The mount namespace isn't entered, so the image needs no tools.
4. **pasta copies the host's interface name**, so the interface comes from the namespace's default route, never assumed to be `eth0`. pasta also splices loopback connections, which therefore aren't shaped.
5. **Host-network containers have no namespace of their own**; their traffic is picked out by local port on a host interface (host mode).
6. **A libvirt VM on a `network` or `bridge` interface has a host-side tap** (`vnetN`), whose egress carries traffic *to* the guest: the opposite of a container's interface (measured).
7. **Measured kernel behaviour** (`SPIKE-RESULTS.md`): netem's simple loss correlation drops almost nothing (not offered; bursty loss uses the Gilbert-Elliott model); outgoing loss is *not* masked from TCP on current kernels; netem's own rate and an HTB class measured the same.
8. **PID reuse.** Targets are resolved to a namespace inode, re-checked before every change.

## 3. Architecture

```
cli.py        commands, multi-target planning, exit codes
profiles.py   built-in and user TOML profiles, validation, netem arguments
target.py     container -> PID -> netns inode, interface, shared-namespace members
backends.py   nsenter backend (container mode)
inspector.py  live container-mode state from the kernel
planner.py    container-mode apply/clear plans; netem_tree() shared by all modes
host.py       host and VM mode: ports, slots, clsact/flower/IFB plans, live state from the kernel
vm.py         libvirt lookup (virsh domiflist) and tap MAC check
state.py      container-mode state file (what the kernel can't tell us)
timer.py      --duration: one-shot systemd timers
```

The planner emits commands; one executor runs them, so `--dry-run` is the same code path as a real apply. Every command is printed with its namespace prefix.

## 4. The three modes

### 4.1 Container mode (`apply`)

Inside the target's namespace, on the interface of its default route:

```
egress  (upload):   root 5753: netem [pfifo child]                        -- or HTB, see 4.4
ingress (download): clsact + matchall pref 49152 -> mirred -> wanshape0 (IFB) root 5753: netem
```

Re-applying rebuilds both trees (`replace` would keep stale children). A failed apply is rolled back unless `--allow-partial`. Containers in a pod or `--network container:X` share one namespace: `apply` names the other members, and the namespace is one state entry, reachable through any member.

### 4.2 Host mode (`apply-host`)

Nothing replaces the interface's root qdisc. A `clsact` qdisc carries `flower` filters; matching packets go to a per-target IFB pair:

```
egress:  match -> mirred -> wsh<N>up (netem up)
ingress: match -> mirred -> wsh<N>dn (netem down)
```

Matches, per IP family (IPv4 and IPv6) and protocol (UDP and TCP):
- **local ports** (`--ports`, a `wanshape.ports` label, or `*SIP_PORT` / `*PORT_LOW`/`*PORT_HIGH` variables): `src_port` out, `dst_port` in;
- **remote ports** (`--remote-ports`, a `wanshape.remote_ports` label): `dst_port` out, `src_port` in, for clients on random local ports;
- **peer** (`--peer`): `dst_ip` out, `src_ip` in, limiting the other matches to one host and one family; on its own it matches all traffic with that host.

Each target takes a slot N (0-9): local-port (and VM) filters use prefs `49300 + 100*N` upwards on both hooks, remote-port and peer-only filters `50300 + 100*N` upwards, so a packet from one target's local port is always that target's even if it also matches another's remote port. Ambiguous pairs on one interface are refused: overlapping local ports, overlapping remote ports, or a peer-only target against another remote-side one, unless they're limited to different peers. `clsact` ownership is shared, so whichever wanshape target is cleared last removes it, unless another tool's filters remain. An interface with a plain `ingress` qdisc (libvirt `<bandwidth><outbound>`) is refused: it can't coexist with `clsact`. Under sudo, rootless containers are inspected as `SUDO_USER`.

### 4.3 VM mode (`apply-vm`)

The host-mode engine with one `matchall` filter per hook on the VM's tap, hooks swapped:

```
tap ingress (from the guest, its upload)  -> wsh<N>up
tap egress  (to the guest, its download)  -> wsh<N>dn
```

The tap comes from `virsh domiflist` and must carry the MAC libvirt gives a guest's tap (`fe:` + the guest's MAC), so a reused `vnetN` name can't redirect the shaping. `direct` (macvtap) interfaces are refused until their hooks are measured (#1); `user`/passt has no host device (use host mode).

### 4.4 The netem tree (all modes)

`planner.netem_tree()` builds every shaped device's qdiscs at handle `5753:`:
- default: netem at the root, doing delay, jitter, loss, duplication, corruption, reordering, slotting and the rate; plus a `pfifo` child when jitter mustn't reorder packets;
- `rate_mode = "accurate"` with a rate: an HTB root whose single class holds the rate, with netem (without its rate) at `5754:` below it.

## 5. State

**The kernel is the source of truth.**
- *Container mode:* wanshape's qdiscs carry handle `5753:`, its ingress filter pref `49152`, its IFB is `wanshape0`. The state file (`$XDG_STATE_HOME/wanshape/state.json`) holds only what the kernel can't: the profile name, the root qdisc found before the first apply, whether wanshape added `clsact`, the namespace inode (to detect a recreated container) and the `--duration` expiry. `status --all` scans running containers for shaping the file doesn't know about; `status --prune` forgets gone or recreated containers.
- *Host and VM modes:* no file. Each target's metadata (name, kind, profile, interface, ports, remote ports, peer, direction, clsact ownership, expiry, and for VMs the device type and guest MAC) is the alias of its `wsh<N>up` IFB, readable without root. A VM slot whose device now carries another guest's MAC (libvirt reused the name) is taken over by the new VM's apply. A target whose device has gone (VM restart) is shown as stale.

## 6. Safety rules

- Resolve, plan and validate **every** target in a command before changing anything; with several targets, undo the ones done if a later one fails (unless `--keep-going`).
- Re-check the namespace inode before each change.
- Host and VM changes (apply, clear, the libvirt hook) hold a lock (`/run/wanshape/lock`, in a root-owned 0700 directory), so two runs can't plan against the same free slot. The libvirt hook waits at most 30 s for it, and only for VMs and operations it acts on.
- A failed apply undoes only what that run created, never a device or filter that was already there.
- Never replace another tool's root qdisc, `clsact` filters or IFB without `--force` (container mode); never touch another tool's filters in host or VM mode.
- Roll back a failed apply.
- Clear removes only what wanshape added, and only removes `clsact` when nothing else uses it.
- Host and VM modes require root; container mode never escalates.

## 7. Lifecycle extras

- **`--duration`**: a one-shot `systemd-run` timer (the user's systemd for rootless containers, the system's as root) runs wanshape's own clear. systemd is checked before any change. `clear`, or applying again without `--duration`, cancels the timer.
- **Several targets per command**: names separated by spaces or commas. Host/VM targets in one list are planned against each other (slots, overlaps, shared `clsact`).

## 8. Testing

- Unit tests for every planner, parser and CLI path, with fakes for the kernel and runtimes.
- Integration tests that measure real round trips: `host_mode.sh` (veth pair in a private user+net namespace, no root), `netns_mode.sh` (rootless Podman bridge, pasta, keep-id, pods, `--duration`), `vm_mode.sh` (a KVM guest on a tap, no root). CI runs them on GitHub Actions.
- `tools/measure-profiles.sh` measures every profile.

## 9. Open work

Tracked as GitHub issues: macvtap VMs (#1), VM-to-VM measurement (#2), re-apply after a VM restart (#3), Docker and the sidecar backend (#4), host and VM `--duration` under sudo on a second machine (#5), PyPI publishing (#14).

## 10. Revision history

- **r9, 2026-11-25**: rewritten to describe the tool as built. Since r8: rollback of failed container applies, `status --all`/`--prune`, shared namespaces, more netem options (loss correlation removed after measurement), fourteen profiles, `--remote-ports`/`--peer`, `--duration`, several targets per command, `--rate-mode accurate`, CI, packaging.
- **r8**: VM mode.
- **r7**: MVP with host mode; clsact + flower + IFB instead of the planned `prio`/`u32` root qdisc; kernel-only state for host mode.
- **r6**: phase-1 prototype (container mode).
- **r5**: phase-0 spike on the laptop; sidecar `--userns` must be conditional.
- **r1-r4**: drafts and review corrections (rootless namespace entry, pasta naming and splicing, reordering as an explicit choice, bursty loss, the CallRaven port ranges, kernel as source of truth).

## References

- `tc-netem(8)`: <https://man7.org/linux/man-pages/man8/tc-netem.8.html> — options, rate caveat, reordering with jitter, `gemodel`/`state` loss models.
- Linux Foundation netem page: <https://wiki.linuxfoundation.org/networking/netem> — the local-loss caveat (not reproduced on current kernels: `SPIKE-RESULTS.md`).
- Podman networking: <https://github.com/containers/podman/blob/main/docs/tutorials/basic_networking.md>; `podman-unshare(1)`: <https://docs.podman.io/en/latest/markdown/podman-unshare.1.html>.
- `user_namespaces(7)`: <https://man7.org/linux/man-pages/man7/user_namespaces.7.html>; `setns(2)` (`EINVAL` joining the caller's own user namespace).
- Entering Podman namespaces: <https://www.gabriel.urdhr.fr/2022/12/03/entering-podman-namespaces/>.
- `pasta(1)`: <https://passt.top/builds/latest/web/passt.1.html>.
- Default capabilities: <https://www.redhat.com/en/blog/privileged-flag-container-engines>.
- IFB / ingress shaping: <https://hechao.li/posts/container-bandwidth-limit>.
- veth qdisc gotcha: <https://github.com/moby/moby/issues/33162>.
- Pumba (sidecar netem, Docker): <https://github.com/alexei-led/pumba>; Podman #19185: <https://github.com/containers/podman/issues/19185>.
