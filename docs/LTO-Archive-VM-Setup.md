# LTO Tape Archive — Linux VM Setup on Dell R740 (Windows Server 2019 Host)

**Purpose:** Replace the unreliable Windows LTFS + ML3 web-interface workflow with a Linux VM
that gets direct SAS control of the H355e HBA, so tape/library operations run via `mtx`/LTFS
instead of the browser.

**Scope:** Once-a-year archive run. Host stays Windows Server 2019 / Hyper-V for everything else;
only the H355e HBA and its tape/library devices are handed to the VM for the duration of the job.

---

## 1. VM Provider Decision

**Use Hyper-V. Do not introduce a second hypervisor (VirtualBox, VMware Workstation, etc.) on this host.**

Reasons:
- Windows Server 2019 already includes Hyper-V as a role — no new licensing, no new attack surface, no
  dual-hypervisor conflicts (nested hypervisors on the same box are unsupported and cause exactly the
  kind of instability you're trying to eliminate).
- **DDA (Discrete Device Assignment)** — the mechanism that hands the whole H355e HBA to a guest — is a
  Hyper-V-specific feature. Third-party desktop hypervisors (VirtualBox, VMware Workstation) don't offer
  an equivalent on Windows Server, and full ESXi would mean wiping the host OS, which is out of scope here.

Action:
1. Confirm the Hyper-V role is installed: `Get-WindowsFeature -Name Hyper-V` (PowerShell, run as admin).
2. If not installed: `Install-WindowsFeature -Name Hyper-V -IncludeManagementTools -Restart`.

---

## 2. Linux Build and Release

**Recommendation: Ubuntu Server 24.04 LTS.**

Reasons:
- Long support window (until 2029) fits a "touch it once a year" cadence — you want the OS still
  receiving security patches with minimal maintenance in between runs.
- `mt-st`, `mtx`, `sg3-utils`, and `ltfs` are all in the standard/universe repos — no third-party
  repos or manual compilation needed.
- Widest driver coverage for SAS HBAs generally, which reduces risk when the H355e's controller chip
  shows up post-DDA-passthrough.

Alternative: Debian 12 (Bookworm) is equally valid if you prefer Debian's slower-moving base — same
package availability. Either is fine; don't spend time deliberating further.

Action:
1. Download the Ubuntu Server 24.04 LTS ISO to the R740 (or a share reachable from it).
2. Create a **Generation 2** VM in Hyper-V Manager (DDA requires Gen 2 — Gen 1 does not support it).
3. Attach the ISO, install Ubuntu Server with a minimal/standard install (no desktop GUI needed).
4. Set a static IP or a DHCP reservation for the VM — you'll want a stable address for SSH/SMB access
   during the run.
5. Update and install the tape toolchain:
   ```
   sudo apt update && sudo apt install -y mt-st mtx sg3-utils ltfs lsscsi
   ```

---

## 3. Memory and Resource Allocation (96GB host total)

Tape/LTFS operations are **I/O-bound, not CPU/RAM-bound** — the bottleneck will be SAS/tape drive
throughput and network transfer from the source machines, not compute. Over-provisioning the VM buys
you nothing here.

Suggested allocation:

| Resource | Allocation | Notes |
|---|---|---|
| vCPU | 4 | LTFS, mtx, and network copy don't need more; leave headroom for host |
| RAM | 16–32GB (start at 16, use Dynamic Memory) | Mainly useful as filesystem cache/staging buffer for the copy step |
| OS/boot disk | 80–100GB (VHDX, dynamically expanding) | OS + tools only |
| Staging disk (optional) | Size to your largest single dataset if you stage-then-copy rather than streaming | See note below |

**Staging vs. streaming:** decide whether the annual job pulls data from the network straight into
`ltfs`-mounted tape, or stages it to local disk first and then copies to tape. Streaming avoids
needing a large staging disk but ties up the tape drive for the full network transfer duration
(risk: any network hiccup interrupts a tape write). Staging needs disk space (could be a mounted
SAN/NAS LUN rather than local VHDX) but decouples network reliability from tape reliability. Given the
value of the archive, staging is the safer default unless the data volume makes it impractical.

Action:
1. In Hyper-V Manager → VM Settings → Memory: enable Dynamic Memory, set Minimum 8GB / Startup 16GB /
   Maximum 32GB.
2. In VM Settings → Processor: set 4 virtual processors.
3. Leave the remaining ~60GB+ RAM and cores free for the Windows host and any other roles it runs.

---

## 4. DDA Hand-Over Process (H355e HBA → VM)

This is the step that actually fixes your instability problem — once done, the VM talks to the drives
and ML3 changer directly over SAS, with no browser/HTTP layer in between.

### 4.1 Pre-checks (do these before touching anything)

1. Confirm IOMMU/VT-d (or AMD-Vi) is enabled in the R740's BIOS (Dell calls it "Virtualization
   Technology for Directed I/O" under System BIOS → Integrated Devices, or similar depending on BIOS
   version). DDA requires this.
2. Confirm the H355e sits in its own IOMMU group with no other essential device sharing that group —
   check with:
   ```powershell
   # Run on the Windows host
   Get-VMHostPartitionableDevice
   ```
   Devices listed here with a clean partitioning are DDA candidates. If the H355e doesn't appear, IOMMU
   isn't enabled correctly or the platform ACS support is insufficient — resolve this before continuing.
3. Confirm nothing else on the host currently depends on that HBA (it should have no other purpose than
   the tape library on this host, per your setup, but worth a final check via Device Manager).

### 4.2 Dismount the HBA from the host

Run as Administrator in PowerShell on the Windows host (VM must be **shut down**, not just saved state):

```powershell
# Identify the HBA's PCI location
$hba = Get-PnpDevice -PresentOnly | Where-Object { $_.FriendlyName -like "*H355e*" -or $_.FriendlyName -like "*SAS*" }
$hba | Select-Object FriendlyName, InstanceId

# Get its location path (needed for DDA cmdlets)
$locationPath = ($hba | Get-PnpDeviceProperty -KeyName DEVPKEY_Device_LocationPaths).Data[0]

# Disable the device on the host
Disable-PnpDevice -InstanceId $hba.InstanceId -Confirm:$false

# Dismount it from the host partition
Dismount-VMHostAssignableDevice -LocationPath $locationPath -Force
```

### 4.3 Assign the HBA to the VM

```powershell
Add-VMAssignableDevice -VMName "TapeArchiveVM" -LocationPath $locationPath
```

### 4.4 Configure VM for DDA compatibility

DDA has a couple of mandatory Gen 2 VM settings:

```powershell
Set-VM -VMName "TapeArchiveVM" -AutomaticStopAction TurnOff
Set-VMFirmware -VMName "TapeArchiveVM" -EnableSecureBoot Off
Set-VMFirmware -VMName "TapeArchiveVM" -Guarded $false
```
(`AutomaticStopAction TurnOff` is required — DDA devices don't support Save-State; the VM must be able
to fully power off/on rather than being saved.)

### 4.5 Start the VM and verify

Boot the VM, then inside Ubuntu:
```bash
lspci | grep -i sas       # confirm the H355e is visible
lsscsi -g                 # should show the two tape drives (type 1) and the ML3 changer (type 8)
```

### 4.6 Returning the HBA to the host (if ever needed)

Shut the VM down, then:
```powershell
Remove-VMAssignableDevice -VMName "TapeArchiveVM" -LocationPath $locationPath
Mount-VMHostAssignableDevice -LocationPath $locationPath
Enable-PnpDevice -InstanceId $hba.InstanceId -Confirm:$false
```

---

## 5. Networking Setup

**Recommendation: dedicated 10G NIC, passed to the VM as its own external virtual switch — not shared
with host management traffic.**

Reasoning:
- The annual job is a single large, time-boxed transfer pulling from multiple machines across the
  network. Sharing a NIC with host management/other roles risks contention exactly during the one
  window a year this matters, and makes it harder to diagnose "is this slow because of tape or because
  of network" if problems occur.
- You already have spare 10G ports, so there's no cost trade-off to dedicating one.
- A dedicated external vSwitch bound to a single physical 10G NIC gives the VM full line-rate access
  without going through DDA (standard Hyper-V networking is fine here — DDA is only needed for the SAS
  HBA, not the NIC).

Action:
1. On the Windows host, identify the spare 10G NIC in Device Manager / `Get-NetAdapter`.
2. Create a dedicated external virtual switch bound to that NIC:
   ```powershell
   New-VMSwitch -Name "TapeArchive-vSwitch" -NetAdapterName "<10G NIC name>" -AllowManagementOS $false
   ```
   `-AllowManagementOS $false` keeps this NIC exclusive to the VM, not shared with the host.
3. Attach the VM's network adapter to this switch:
   ```powershell
   Connect-VMNetworkAdapter -VMName "TapeArchiveVM" -SwitchName "TapeArchive-vSwitch"
   ```
4. Inside Ubuntu, configure a static IP on that interface (via netplan) so the source machines/shares
   can reach it predictably.
5. Confirm the source machines' network paths (switch ports, VLANs) actually route to this NIC — if the
   10G ports sit on a separate VLAN or switch from where the source data lives, loop in whoever manages
   the network gear before the day of the run.

---

## 6. Setup and Initial Test With the Actual Drives and Library

Do this as a dry run **before** the real annual job, with a scratch/test tape — not a tape from the
existing archive set.

1. **Confirm device enumeration:**
   ```bash
   lsscsi -g
   ```
   Expect: 1 changer (`mediumx`), 2 tape drives (`tape`). Note the `/dev/sg*` and `/dev/nst*` device
   paths — these can shift on reboot, so consider udev rules for stable names if you'll script against
   them (`/dev/tape/by-id/...` is often already populated on Ubuntu).

2. **Test changer control:**
   ```bash
   sudo mtx -f /dev/sg<changer_node> status
   ```
   This should list all slots and drives with occupancy — this replaces the ML3 web page entirely.

3. **Load a test tape:**
   ```bash
   sudo mtx -f /dev/sg<changer_node> load <slot_number> <drive_number>
   ```

4. **Confirm drive sees the tape:**
   ```bash
   sudo mt -f /dev/nst<drive_number> status
   ```

5. **Format for LTFS (only if using a fresh/scratch tape — do NOT run this against an existing
   archive tape):**
   ```bash
   sudo mkltfs -d /dev/nst<drive_number>
   ```

6. **Mount via LTFS and test read/write:**
   ```bash
   sudo mkdir -p /mnt/ltfs
   sudo ltfs -o devname=/dev/nst<drive_number> /mnt/ltfs
   # copy a small test file in, verify it, then:
   sudo umount /mnt/ltfs
   ```

7. **Test unload back to the library:**
   ```bash
   sudo mtx -f /dev/sg<changer_node> unload <slot_number> <drive_number>
   ```

8. **Network throughput sanity check:** from the VM, pull a representative sample dataset from one of
   the source machines over the dedicated 10G link (e.g. `iperf3` for raw throughput, then an actual
   `rsync`/`cp` test) to confirm you're getting expected speeds before the real run, when the volume
   will be much larger.

9. **Full dry run:** repeat steps 3–7 for both drives, and confirm the changer can address both drives
   correctly (some libraries number drives 0/1 differently in `mtx status` output vs. physical bay
   position — verify this now, not on the day).

Once all of the above passes cleanly on a scratch tape, you're ready to script the real pipeline
(load → LTFS mount → copy from network sources → verify → unmount → unload → next tape).
