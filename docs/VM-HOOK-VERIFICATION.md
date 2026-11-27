# VM hook verification: live libvirt restart

> **Not verified yet.** The code is in place (`apply-vm --persist`, the
> `/etc/libvirt/hooks/qemu.d/wanshape` hook, `install-vm-hook` /
> `uninstall-vm-hook`), but the real-libvirt restart test with delay
> measurement is **still pending**. This is a checklist for that test, not a
> report of results. Issue #3 stays open until the evidence below is attached.

The hook re-applies the persisted setting when libvirt starts a VM. It reads the
new tap from the domain XML passed on stdin (never `virsh`, which can deadlock
from inside a hook), logs to stderr, and must always exit 0 so it can't fail a
VM start.

## Prerequisites

- Root on the libvirt host (`sudo`), with libvirt running and `virsh` available.
- A VM with a `network`, `bridge` or `direct` (macvtap) interface. For macvtap,
  measure from another VM or machine: the host can't reach a macvtap guest.
- `tc` and `ip` (iproute2), `virsh`, and Python 3.11+ for the installed
  `wanshape` command.
- A profile with a known one-way delay (`wanshape show <profile>`) and a way to
  measure RTT to the guest (e.g. `ping`).

## Checklist

- [ ] Install the hook once: `sudo wanshape install-vm-hook`. Confirm it is
      executable: `ls -l /etc/libvirt/hooks/qemu.d/wanshape` (`-rwxr-xr-x`).
      Restart `virtqemud` (or `libvirtd` on older hosts) if libvirt hasn't
      picked up a newly created hooks directory.
- [ ] Persist the setting:
      `sudo wanshape apply-vm <domain> --profile <profile> --persist`.
      Check the record with `cat /etc/wanshape/vms.json`: it must contain the
      domain, the resolved profile, direction, guest MAC and device type.
- [ ] Record the baseline: RTT to the guest (e.g. `ping -c 20 -q <guest-ip>`,
      median and p95) and `wanshape status` (target, device, profile).
- [ ] Restart the VM through libvirt: `virsh destroy <domain> && virsh start
      <domain>` (or `virsh reboot <domain>`). Confirm libvirt recreated the
      interface with `virsh domiflist <domain>` and `ip -br link show`; the tap
      may have a new `vnetN` name.
- [ ] Check `wanshape status` again: the target must show the **new** device,
      not STALE. Measure RTT the same way as the baseline. Expect roughly the
      profile's one-way delay added in each direction (about twice that delay
      on the round trip, within jitter and measurement noise).
- [ ] Confirm the hook neither blocked nor failed the start: `virsh start`
      returned promptly, `virsh domstate <domain>` is `running`, and the guest
      answers. Inspect libvirt's log (`journalctl -u virtqemud` or
      `/var/log/libvirt/libvirtd.log`) for the
      `wanshape vm-hook <domain> started begin: re-applied to <tap>` line and
      for any wanshape error. A non-persisted VM should log "not a wanshape
      VM" and still start.
- [ ] Clean up: `sudo wanshape clear-vm <domain>` (forgets the persisted entry
      and removes the shaping) and, if no other VM uses it,
      `sudo wanshape uninstall-vm-hook`. Confirm `wanshape status` no longer
      lists the target and no `vnetN` tap has wanshape's `clsact` qdisc.

## Evidence to capture

Attach all of it to #3 so the issue can be closed with proof:

- Date, hostname, kernel and distro (`date -Is`, `hostname`, `uname -a`,
  `/etc/os-release`), libvirt version (`virsh --version`).
- The install commands and their output: `ls -l /etc/libvirt/hooks/qemu.d/wanshape`
  and `cat /etc/wanshape/vms.json`.
- `virsh domiflist <domain>` and `ip -br link show` before and after the
  restart, naming the old and new tap.
- `wanshape status` before and after, showing shaping on the new tap.
- RTT before and after with the profile's one-way delay noted for comparison.
- The libvirt log snippet with the hook's re-apply line, plus the `virsh start`
  command and its exit status/elapsed time.
- Output of the cleanup commands and a final `wanshape status`.

## What is already covered

`tests/test_vmhook.py` unit-tests the XML tap lookup, the persisted profile
round trip and hook dispatch. `tests/integration/vm_mode.sh` exercises the
installed hook against a real KVM guest, but its `virsh` is a stand-in and its
"restart" creates a simulated tap: **there is no real libvirt hook invocation
or real libvirt restart in CI.** That is the gap this checklist closes.
