# Security policy

wanshape changes the Linux networking of the machine it runs on. Host and VM
modes run as root (`sudo`), and `install-vm-hook` puts a hook in libvirt that
runs as root whenever a VM starts. Please report anything that could let it
change more than the target you asked for, run something you didn't intend,
or be abused by another local user, privately.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: the repository's **Security**
tab › **Report a vulnerability**
(<https://github.com/chrisnock-personal/wanshape/security/advisories/new>).
Only the maintainer sees the report.

Please don't open a public issue, pull request or discussion for it.

Useful to include: the version (`python3 -c "import wanshape; print(wanshape.__version__)"`
or the commit), the distribution and kernel, which mode (`apply`, `apply-host`,
`apply-vm`, the libvirt hook), and steps to reproduce. You'll get a reply within
a week. Once a fix is released, the advisory is published with credit, unless
you'd rather not be named.

## Supported versions

Fixes go into the latest release only.

| Version | Supported |
|---|---|
| Latest release | Yes |
| Anything older | No: update to the latest |

## How it's built

In short (details in [docs/DESIGN.md](docs/DESIGN.md)):

- **No shell.** Every `ip`, `tc`, `nsenter`, `virsh` and `systemd-run` call is an
  argument list, never a shell string. Target and profile names are limited to
  letters, digits, `.`, `_` and `-`; a profile's `raw` netem arguments go only
  to `tc`.
- **Least privilege.** Your own rootless containers need no root: wanshape
  enters their namespaces as their owner. Root is required only for host and
  VM modes, and wanshape never calls `sudo` itself.
- **Only what it was asked to change.** Every change is printed first
  (`--dry-run` stops there). wanshape refuses to replace another tool's qdiscs
  or filters, rolls back a failed apply, undoes only what that run created,
  and removes only what it added. Host and VM changes are serialised with a lock.
- **The libvirt hook never blocks a VM.** It reads the VM's device from the XML
  libvirt passes in (it never calls back into libvirt), always exits 0, and only
  acts on VMs recorded with `--persist` in `/etc/wanshape/vms.json` (root-owned,
  mode 0644).
- **No dependencies.** The Python standard library only, so there are no
  third-party packages to keep patched. CI runs lint, unit tests and the
  integration tests on every push; Dependabot keeps the GitHub Actions current.

Out of scope: the security of the kernel, `tc`/`iproute2`, Podman, Docker,
libvirt and systemd themselves, and anything an attacker can do once they
already have root.
