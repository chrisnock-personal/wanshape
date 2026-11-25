# wanshape

Apply netem/tc WAN impairments to **one named container**, on Podman and Docker, without changing the container image.

This is an early prototype (v0.1.0). It implements the phase-1 core of [`docs/DESIGN.md`](docs/DESIGN.md): target resolution, nsenter backend, live inspection, profiles, dry-run, apply, status and clear. Read [`docs/SPIKE-RESULTS.md`](docs/SPIKE-RESULTS.md) for the phase-0 validation already done on the laptop.

## Requirements

- Linux with `tc`, `ip`, `nsenter`, and `podman` and/or `docker`.
- Kernel support for `sch_netem`, `ifb`, `act_mirred`, `cls_matchall` (normally autoloaded on demand).
- Python 3.11+ (no third-party packages).
- Rootless Podman targets must be owned by the invoking user. Rootful targets require the caller to already be root. The sidecar backend is designed but not implemented in this prototype.

Run `wanshape doctor` to check the host.

## Install (editable)

```sh
python3 -m pip install -e .
```

Or run directly from the checkout:

```sh
PYTHONPATH=src python3 -m wanshape.cli --help
```

## Usage

```sh
wanshape list
wanshape show 3g

# Print the exact commands without changing anything.
wanshape apply my-container --profile 3g --dry-run

# Both directions by default; delay is one-way per direction.
wanshape apply my-container --profile 3g
wanshape apply my-container --delay 150ms --jitter 30ms --loss 1% --rate 768kbit

# One direction only.
wanshape apply my-container --delay 100ms --direction ingress

# Read live state from the kernel (state file only adds the profile name).
wanshape status
wanshape status my-container

wanshape clear my-container
```

`--force` replaces a foreign root qdisc or clsact/IFB state. Without it, wanshape refuses to clobber another tool's configuration. `--no-reorder` attaches a `pfifo` child under netem so jitter does not reorder packets.

User profiles are TOML, overlaid on the built-ins:

```sh
mkdir -p ~/.config/wanshape
cat > ~/.config/wanshape/profiles.toml <<'EOF'
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
EOF
```

## Limitations

- **Host-network containers have no per-container netns.** `apply` refuses them. The port-filter `apply-host` mode in the design is not implemented yet.
- Docker support is present in the resolver but has not been tested here (Docker is not installed on the development laptop).
- WSL2 kernel behaviour still needs verification; the netem/IFB spike results are from Fedora on the laptop.
- `--duration` is accepted only to give a clear "not implemented" error.
- The sidecar backend, shared-netns/pod handling and per-flow filters are future phases.

## Tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
