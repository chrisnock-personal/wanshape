# wanshape

Apply netem/tc WAN impairments (latency, jitter, packet loss including bursty loss, bandwidth) to **one container, one service's ports, or one libvirt virtual machine**, without changing the container image or the VM, and without touching the rest of the machine's traffic.

Three modes:

| Mode | For | How | Needs |
|---|---|---|---|
| `apply` | containers with their own network (bridge, pasta, slirp4netns) | netem inside the container's network namespace, entered from the host with the host's `tc` | nothing for your own rootless containers; root for rootful ones |
| `apply-host` | **host-network containers**, or any set of local ports | `flower` filters on a host interface pick out the target's ports and send only those packets through netem | root (`sudo`) |
| `apply-vm` | **libvirt VMs** on a `network` or `bridge` interface | all traffic on the VM's host-side tap device (vnetN) goes through netem | root (`sudo`) |

Status: an MVP (v0.3.0). The container and host modes are tested end to end on Fedora and on WSL2 (Microsoft's 6.6 kernel) with rootless Podman, and host mode has been used on live SIP calls between two host-network CallRaven containers through FreePBX ([results](docs/SPIKE-RESULTS.md#wsl2-and-live-callraven-test--the-pc-2026-11-25)). VM mode is tested on libvirt VMs (NAT network) under WSL2, including a VM restart. Docker has not been tested yet. Design and background: [`docs/DESIGN.md`](docs/DESIGN.md), [`docs/SPIKE-RESULTS.md`](docs/SPIKE-RESULTS.md).

## Requirements

- Linux with `tc` and `ip` (iproute2), `nsenter`, and `podman` and/or `docker`.
- For VM mode: libvirt's `virsh`, and VMs on a `network` or `bridge` interface.
- Kernel support for `sch_netem`, `ifb`, `act_mirred`, `cls_matchall` and (host mode) `cls_flower`, built in or loadable. They normally load on first use.
- Python 3.11+, no third-party packages.

Run `wanshape doctor` to check the host.

## Install

Run it from the checkout:

```sh
./bin/wanshape --help
sudo ./bin/wanshape apply-host ...      # host mode needs root
```

Or install it for your user with `python3 -m pip install --user -e .`. That copy isn't on root's path, so use `./bin/wanshape` with sudo.

## Profiles

```sh
wanshape list
wanshape show lossy-sip
```

| Profile | One-way delay | Jitter | Loss | Down / up | Reorders |
|---|---|---|---|---|---|
| `office` | 5 ms | 1 ms | 0% | 100 / 100 mbit | no |
| `4g` | 25 ms | 10 ms | 0.1% | 10 / 5 mbit | no |
| `3g` | 150 ms | 30 ms | 1% | 1600 / 768 kbit | yes |
| `satellite` | 300 ms | 20 ms | 0.5% | 5 / 1 mbit | no |
| `bad-wifi` | 40 ms | 60 ms pareto | 5% | 2 / 1 mbit | yes |
| `lossy-sip` | 50 ms | 15 ms | bursty (Gilbert-Elliott) | 512 / 512 kbit | no |

**Delay is one-way and applies in each direction**, so `3g` adds about 300 ms to a round trip. "Up" (egress) is traffic from the target; "down" (ingress) is traffic to it. With jitter, netem reorders packets unless the profile says otherwise or you pass `--no-reorder`.

Instead of a profile, give the settings inline: `--delay 150ms [--jitter 30ms] [--distribution normal] [--loss 1% | --loss-model gemodel:1%,25%,100%,0%] [--rate 768kbit]`. Inline settings apply to both directions.

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

## Per-container mode: `apply`

```sh
# Print the exact commands without changing anything.
wanshape apply my-container --profile 3g --dry-run

wanshape apply my-container --profile 3g                  # both directions
wanshape apply my-container --delay 100ms --direction ingress
wanshape status [my-container]                            # read from the kernel, with counters
wanshape clear my-container
```

Applying again replaces the settings. `--force` replaces another tool's root qdisc or ingress filter; without it, wanshape refuses to change them.

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

wanshape status                            # no root needed
sudo ./bin/wanshape clear-host callraven
sudo ./bin/wanshape clear-host --all
```

The dry run prints the ports and where they came from. Check them: a port the container uses but doesn't declare is not shaped. Run it as `sudo`: wanshape then inspects your rootless containers as you.

Things to know:

- **Local ports only.** A connection from a random local port isn't matched, even if the far end's port is well known. For example, SIP over WebSocket from a browser-style client uses a random local port, so only its RTP is shaped.
- **Loopback isn't shaped.** Traffic between processes on the same host never reaches the interface.
- **One interface per target.** Pick the one that carries the traffic you care about, for example with `--via <peer address>`.
- Up to 10 host targets at once. Their ports must not overlap on the same interface.
- wanshape adds a `clsact` qdisc to the interface if there isn't one and removes it when the last host target is cleared, unless another tool has filters there.
- Host mode keeps no state file. Each target's settings are stored in the alias of its `wsh<N>up` IFB device, so `status` and `clear-host` read them from the kernel.

## VM mode: `apply-vm`

Shapes everything a libvirt VM sends and receives on one interface, including traffic to other VMs on the same bridge. wanshape asks libvirt which host device the VM uses (`virsh domiflist`) and checks that the device's MAC is the one libvirt gives that VM's tap.

```sh
sudo ./bin/wanshape apply-vm freepbx --profile lossy-sip --dry-run
sudo ./bin/wanshape apply-vm freepbx --profile lossy-sip
sudo ./bin/wanshape apply-vm web --mac 52:54:00:aa:bb:02 --delay 80ms   # VM with several NICs
sudo ./bin/wanshape apply-vm devbox -c qemu:///session --delay 50ms     # another libvirt URI
wanshape status
sudo ./bin/wanshape clear-vm freepbx
```

`--direction egress` is the VM's upload (what it sends), `ingress` its download. It uses the same engine as host mode: the VM's tap gets a `clsact` qdisc and a pair of IFBs, its root qdisc is untouched, and the state is kept in the kernel.

- **Supported:** `network` and `bridge` interfaces (and `ethernet`), which use a tap device. **Not yet:** `direct` (macvtap) interfaces, which are refused until the direction of their tc hooks is measured, and `user`/passt networking (no host device; use `apply-host --ports` for the ports it forwards).
- **A VM restart loses the shaping**, because libvirt recreates the tap, often under a new name. `status` marks the target STALE; run `apply-vm` again.
- If the VM's interface has a libvirt `<bandwidth><outbound>` limit, libvirt has put an `ingress` qdisc on the tap that can't coexist with `clsact`, and wanshape refuses (exit 4).

## Exit codes

0 success, 1 error, 2 unsupported target (e.g. `apply` on a host-network container), 3 prerequisite missing (e.g. host mode without root), 4 conflict (another tool's tc state, or overlapping host targets).

## Limitations

- Docker support is written but untested (Docker isn't installed on the development machine).
- A rootful container needs root (`sudo`); the sidecar backend for non-root Docker users is designed but not built.
- Shaping is lost when a container restarts (its network namespace goes away). `status` reports such entries as stale.
- `--duration` (automatic clear) is not implemented.
- Loss applied to outgoing TCP is reported to the local TCP stack, which resends at once; for realistic TCP loss, shape the download direction. UDP (RTP, SIP) behaves as expected both ways.

## Tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v   # unit tests, no privileges

sh tests/integration/host_mode.sh     # host mode on a veth pair in a private user+net namespace; no root needed
sh tests/integration/netns_mode.sh    # rootless Podman bridge, pasta and keep-id containers; needs the python:3.12-slim image
WANSHAPE_VM_IMAGE=cirros-0.6.2-x86_64-disk.img sh tests/integration/vm_mode.sh   # KVM guest on a tap, no root
```

The integration tests measure real round-trip times with and without shaping, and check that everything is removed afterwards.
