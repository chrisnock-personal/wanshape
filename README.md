# wanshape

[![CI](https://github.com/chrisnock-personal/wanshape/actions/workflows/ci.yml/badge.svg)](https://github.com/chrisnock-personal/wanshape/actions/workflows/ci.yml)

Apply netem/tc WAN impairments (latency, jitter, packet loss including bursty loss, bandwidth) to **one container, one service's ports, or one libvirt virtual machine**, without changing the container image or the VM, and without touching the rest of the machine's traffic.

Three modes:

| Mode | For | How | Needs |
|---|---|---|---|
| `apply` | containers with their own network (bridge, pasta, slirp4netns) | netem inside the container's network namespace, entered from the host with the host's `tc` | nothing for your own rootless containers; root for rootful ones |
| `apply-host` | **host-network containers**, or any set of local ports | `flower` filters on a host interface pick out the target's ports and send only those packets through netem | root (`sudo`) |
| `apply-vm` | **libvirt VMs** on a `network`, `bridge` or `direct` (macvtap) interface | all traffic on the VM's host-side tap or macvtap device goes through netem | root (`sudo`) |

Status: early but working (v0.4.0). Container and host modes are tested end to end on Fedora, on WSL2 (Microsoft's 6.6 kernel) and on GitHub's Ubuntu runners, with rootless Podman and rootful Docker; host mode has been used on live SIP calls between two host-network CallRaven containers through FreePBX ([results](docs/SPIKE-RESULTS.md#wsl2-and-live-callraven-test--the-pc-2026-11-25)). VM mode is tested on libvirt VMs under WSL2: tap and macvtap interfaces, VM-to-VM traffic, a VM restart and `--duration`. Design and background: [`docs/DESIGN.md`](docs/DESIGN.md), [`docs/SPIKE-RESULTS.md`](docs/SPIKE-RESULTS.md).

## Requirements

- Linux with `tc` and `ip` (iproute2), `nsenter`, and `podman` and/or `docker`.
- For VM mode: libvirt's `virsh`, and VMs on a `network`, `bridge` or `direct` (macvtap) interface.
- Kernel support for `sch_netem`, `ifb`, `act_mirred`, `cls_matchall` and (host mode) `cls_flower`, built in or loadable. They normally load on first use.
- Python 3.11+, no third-party packages.

Run `wanshape doctor` to check the host.

## Install

Into its own environment, with [pipx](https://pipx.pypa.io/) (or `uv tool install`):

```sh
pipx install git+https://github.com/chrisnock-personal/wanshape
wanshape doctor
sudo "$(command -v wanshape)" apply-host ...     # host and VM modes need root
```

`sudo` resets `PATH`, so give it the full path as above; a pipx install keeps working under sudo because its environment doesn't depend on your home directory. (A `pip install --user` copy doesn't: under sudo it can't find its own package.)

Or run it straight from a checkout, no install: `./bin/wanshape ...` and `sudo ./bin/wanshape ...`.

## Profiles

```sh
wanshape list
wanshape show lossy-sip
```

Fourteen built-in profiles, from `office` (5 ms each way) and `transatlantic` through `leo-satellite`, `4g`, `lte-congested`, `wifi-roaming`, `dsl`, `3g` and `2g-edge` to GEO `satellite` (300 ms each way), plus graded VoIP conditions: `lossy-sip`, `voip-poor`, `voip-bad` and `bad-wifi`. [docs/PROFILES.md](docs/PROFILES.md) lists each one's settings, the round trip and loss it measured, and where its numbers come from.

**Delay is one-way and applies in each direction**, so `3g` adds about 300 ms to a round trip. "Up" (egress) is traffic from the target; "down" (ingress) is traffic to it. With jitter, netem reorders packets unless the profile says otherwise or you pass `--no-reorder`.

Instead of a profile, give the settings inline: `--delay 150ms [--jitter 30ms] [--distribution normal] [--loss 1% | --loss-model gemodel:1%,25%,100%,0%] [--rate 768kbit]`. Inline settings apply to both directions.

More netem options, as CLI flags (inline settings) or profile fields:

| Flag | Profile field | What it does |
|---|---|---|
| `--delay-correlation 25%` | `delay_correlation` | each delay depends partly on the last, so jitter drifts instead of jumping (needs `--jitter`) |
| `--reorder 25%,50%` | `reorder = "25% 50%"` | send this share of packets at once, ahead of the delayed ones (needs `--delay`) |
| `--duplicate 1%` | `duplicate` | send some packets twice |
| `--corrupt 0.1%` | `corrupt` | flip one bit in some packets |
| `--slot 10ms,30ms` | `slot = "10ms 30ms"` | hold packets and release them in bursts, as Wi-Fi and LTE schedulers do |
| `--seed 42` | `seed` | fixed random seed, for runs you can repeat (needs a recent iproute2: Fedora 44's 6.17 has it, Ubuntu 24.04's 6.1 doesn't) |
| `--raw-netem "ecn"` | `raw` | any other netem arguments, appended as given |
| `--rate-mode accurate` | `rate_mode = "accurate"` (profile level) | HTB enforces the rate and netem adds the rest, instead of netem's own rate |

`--rate-mode accurate` exists because tc-netem(8) warns that netem's own rate is affected by timer granularity. On Fedora's 7.3 kernel the two measured the same (TCP 3.76 vs 3.77 mbit/s at a 4 mbit/s cap, identical UDP loss and jitter), so it's opt-in, for kernels or VMs with coarser timers.

Your own profiles go in `~/.config/wanshape/profiles.toml` and are added to the built-ins (or override them):

```toml
[profiles.my-sip]
description = "Office Wi-Fi with bursty loss"
reorder_on_jitter = false
[profiles.my-sip.up]
delay = "40ms"
jitter = "10ms"
distribution = "normal"
loss_model = "gemodel 1% 25% 100% 0%"
rate = "1mbit"
[profiles.my-sip.down]
delay = "40ms"
jitter = "10ms"
distribution = "normal"
loss_model = "gemodel 1% 25% 100% 0%"
rate = "2mbit"
```

## Several targets at once

Every command takes one or more targets, separated by spaces or commas:

```sh
wanshape apply web api worker --profile 3g
sudo ./bin/wanshape apply-host callraven,callraven-agent --profile lossy-sip
sudo ./bin/wanshape apply-vm pbx sbc --profile satellite
wanshape status web api
wanshape clear web,api,worker
```

wanshape resolves and plans every target before it changes anything, so an unknown name, a stopped container or a clash between two targets in the list stops the command with nothing changed. If applying one target then fails, the ones already done are undone too; `--keep-going` keeps them instead. Containers in the list that share a network namespace are shaped once. For host mode, `--ports`, `--remote-ports` and `--peer` describe a single target; with several names, each container's ports come from its own label or environment.

## Per-container mode: `apply`

```sh
# Print the exact commands without changing anything.
wanshape apply my-container --profile 3g --dry-run

wanshape apply my-container --profile 3g                  # both directions
wanshape apply my-container --delay 100ms --direction ingress
wanshape apply my-container --profile 3g --duration 10m    # clears itself after 10 minutes
wanshape status [my-container]                            # read from the kernel, with counters
wanshape status --all                                     # also scan every running container
wanshape status --prune                                   # forget containers that are gone
wanshape clear my-container
```

`--duration` (on `apply`, `apply-host` and `apply-vm`) sets a one-shot systemd timer that runs wanshape's own `clear`: your user's systemd for rootless containers, the system's under sudo. `status` shows the time left; `clear`, or applying again without `--duration`, cancels it.

Applying again replaces the settings. `--force` replaces another tool's root qdisc or ingress filter; without it, wanshape refuses to change them.

If one direction fails (for example the kernel has no `ifb` module, so incoming traffic can't be shaped), wanshape undoes everything it set and the container is left unshaped. `--allow-partial` keeps the direction that worked instead.

`status` lists the containers in wanshape's state file (`~/.local/state/wanshape/state.json`). `status --all` also checks every running container you can shape, so it finds shaping applied by another user, by root, or before the state file was lost. `status --prune` forgets entries whose container was removed or recreated.

Containers in a Podman pod, or started with `--network container:X`, share one network namespace: shaping one of them shapes all of them. `apply` says which other containers are affected, and the namespace has one state entry, so `status` and `clear` work through any member.

With pasta (rootless Podman's default network), connections from the host to a published port on `127.0.0.1` skip the container's interface and are **not shaped**. Reach the container on a non-loopback address.

## Host mode: `apply-host`

A host-network container has no network namespace of its own, so wanshape picks out its traffic on a host interface by **local port**: packets leaving from those ports and packets arriving to them, UDP and TCP, IPv4 and IPv6. Other traffic on the interface is not delayed or queued differently, and the interface's root qdisc is not touched.

```sh
# Ports from the container: a wanshape.ports label, or its *SIP_PORT and
# *PORT_LOW/*PORT_HIGH environment variables (e.g. SIP_PORT, RTP_PORT_LOW/HIGH).
sudo ./bin/wanshape apply-host callraven --profile lossy-sip --dry-run
sudo ./bin/wanshape apply-host callraven --profile lossy-sip

# Choose the interface: the default route's unless given.
sudo ./bin/wanshape apply-host callraven --profile 3g --via 192.168.122.167   # route to the PBX
sudo ./bin/wanshape apply-host callraven --profile 3g --iface eth0

# Explicit ports; then the name is only a label and no container is needed.
sudo ./bin/wanshape apply-host softphone --ports 5060,10000-20000 --delay 80ms --loss 2%
sudo ./bin/wanshape apply-host callraven --remote-ports 8088 --peer 192.168.1.167 --profile 3g   # plus its WebSocket SIP
sudo ./bin/wanshape apply-host pbx-leg --peer 192.168.1.167 --profile voip-bad                     # everything with one host

wanshape status                            # no root needed
sudo ./bin/wanshape clear-host callraven
sudo ./bin/wanshape clear-host --all
```

The dry run prints the ports and where they came from. Check them: a port the container uses but doesn't declare is not shaped. Run it as `sudo`: wanshape then inspects your rootless containers as you.

Things to know:

- **Local ports by default.** A connection from a random local port isn't matched by `--ports`. For those, add `--remote-ports`: SIP over WebSocket to a PBX's port 8088, for example, with `--remote-ports 8088` (or a `wanshape.remote_ports` label on the container).
- **`--peer ADDRESS` limits everything to one remote host**, such as the PBX. On its own (no ports) it shapes all traffic with that host, which is the simplest way to put one leg of a call on a bad network.
- **Loopback isn't shaped.** Traffic between processes on the same host never reaches the interface.
- **One interface per target.** Pick the one that carries the traffic you care about, for example with `--via <peer address>`.
- Up to 10 host targets at once. Their ports must not overlap on the same interface.
- wanshape adds a `clsact` qdisc to the interface if there isn't one and removes it when the last host target is cleared, unless another tool has filters there.
- Host mode keeps no state file. Each target's settings are stored in the alias of its `wsh<N>up` IFB device, so `status` and `clear-host` read them from the kernel.

## VM mode: `apply-vm`

Shapes everything a libvirt VM sends and receives on one interface, including traffic to other VMs on the same bridge. wanshape asks libvirt which host device the VM uses (`virsh domiflist`) and checks that the device's MAC belongs to that VM (libvirt gives a tap the guest's MAC with `fe` as the first byte; a macvtap device has the guest's MAC itself).

```sh
sudo ./bin/wanshape apply-vm freepbx --profile lossy-sip --dry-run
sudo ./bin/wanshape apply-vm freepbx --profile lossy-sip
sudo ./bin/wanshape apply-vm web --mac 52:54:00:aa:bb:02 --delay 80ms   # VM with several NICs
sudo ./bin/wanshape apply-vm devbox -c qemu:///session --delay 50ms     # another libvirt URI
wanshape status
sudo ./bin/wanshape clear-vm freepbx
```

`--direction egress` is the VM's upload (what it sends), `ingress` its download. It uses the same engine as host mode: the VM's device gets a `clsact` qdisc and a pair of IFBs, its root qdisc is untouched, and the state is kept in the kernel.

- **Supported:** `network` and `bridge` interfaces (and `ethernet`), which use a tap device, and `direct` interfaces, which use a macvtap device (whose hooks run the other way round; wanshape handles that). Traffic between VMs on the same bridge or macvtap parent is shaped too. **Not supported:** `user`/passt networking, which has no host device; use `apply-host --ports` for the ports it forwards. A macvtap guest can't be reached from its own host (a macvtap limitation), so test it from another VM or machine.
- **A VM restart loses the shaping**, because libvirt recreates the tap, often under a new name. `status` marks the target STALE; run `apply-vm` again, or let wanshape do it: `apply-vm --persist` records the VM's settings in `/etc/wanshape/vms.json`, and a libvirt hook installed once with `sudo wanshape install-vm-hook` (then restart `virtqemud` or `libvirtd`) re-applies them whenever libvirt starts the VM. `clear-vm` forgets them; `uninstall-vm-hook` removes the hook.
- If the VM's interface has a libvirt `<bandwidth><outbound>` limit, libvirt has put an `ingress` qdisc on the tap that can't coexist with `clsact`, and wanshape refuses (exit 4).

## Exit codes

0 success, 1 error, 2 unsupported target (e.g. `apply` on a host-network container), 3 prerequisite missing (e.g. host mode without root), 4 conflict (another tool's tc state, or overlapping host targets).

## Limitations

- A rootful container (Docker, or rootful Podman) needs root (`sudo`). A sidecar-container backend for docker-group users without sudo was designed but isn't planned: with root, wanshape already shapes rootful containers directly.
- Shaping is lost when a container restarts (its network namespace goes away). `status` reports such entries as stale.
- Loss on outgoing traffic behaves like real network loss for both TCP and UDP: measured, TCP retransmitted about as much as with the same loss at the receiver, and UDP lost about the configured share ([measurement](docs/SPIKE-RESULTS.md#loss-on-the-way-out-tcp-and-udp--laptop-2026-11-25)).

## Tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v   # unit tests, no privileges

sh tests/integration/host_mode.sh     # host mode on a veth pair in a private user+net namespace; no root needed
sh tests/integration/netns_mode.sh    # rootless Podman bridge, pasta and keep-id containers; needs the python:3.12-slim image
WANSHAPE_VM_IMAGE=cirros-0.6.2-x86_64-disk.img sh tests/integration/vm_mode.sh   # KVM guest on a tap, no root
sudo sh tests/integration/docker_mode.sh   # rootful Docker containers; needs root and Docker
sh tools/measure-profiles.sh [profile ...]  # what each profile does to an RTP-like stream
```

The integration tests measure real round-trip times with and without shaping, and check that everything is removed afterwards. CI runs all of them on GitHub Actions (`.github/workflows/ci.yml`).

## License

MIT. See [LICENSE](LICENSE).
