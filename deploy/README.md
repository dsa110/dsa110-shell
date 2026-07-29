# DSA-110 node deployment & rebuild procedure

Written 2026-07-29 after the power outage that destroyed `lxd110h20`'s NVMe,
by auditing the live system. Everything here was verified against the running
cluster, not copied from older docs.

This directory holds the **archived copies** of the scripts and MaaS
configuration that build a node. The live originals live on other hosts (see
"Where the originals live"); those hosts are single points of failure, which is
why these copies exist.

---

## 1. What builds what

| Layer | Provided by | Lives on |
| --- | --- | --- |
| Bare-metal OS install | **MaaS 2.8** (region+rack, DHCP, DNS, proxy) | `dsa110maas` — an **LXD container on `lxd110maas`** |
| First-boot node build | **curtin preseed** `curtin_userdata_ubuntu_amd64_generic_bionic` | `dsa110maas:/etc/maas/preseeds/` |
| Build artifacts (tarballs, CUDA, Anaconda) | **nginx** at `http://10.42.0.3/maas/` | **`lxd110maas`** (the physical host) |
| Repo checkout set | `myrepos` + `.mrconfig_production` | this repo |
| Code deployment / updates | `deploy`, `deploy_screen.bash` | this repo, run from `dsa110maas` |

**Networks:** `10.40.0.x` = ant, `10.41.0.x` = sas (data fabric, MTU 9000),
`10.42.0.x` = pro (management). `dsa110maas` is `.4` on each; `lxd110maas` is
`.3` on each.

---

## 2. Rebuilding a corr/search node from bare metal

1. **Provision via MaaS.** Machine boots PXE → commission → deploy
   `ubuntu/bionic`. Storage layout is MaaS default **`flat`**: the whole NVMe,
   one ext4 partition, mounted `/`. There is no LVM/RAID and no per-machine
   layout — the large data disks are not managed by MaaS at all.
2. **curtin runs automatically** (`deploy/maas/curtin_userdata_ubuntu_amd64_generic_bionic`).
   It apt-installs the base set (incl. `myrepos`, `zfsutils-linux`, `syslog-ng`,
   `ntp`), points NTP at the local servers, creates user `ubuntu` with
   `~/proj` and `~/data`, then runs these first-boot scripts in order:
   `00_tarballs.sh` → `00_clone_dsa_repos.sh` → `00_anaconda3.sh` →
   `00_sysctl.sh` → `00_install-cuda-1804.sh` (CUDA 11.1) →
   `00_0_lxd_install_service.sh` → `00_1_lxd_init_cluster.sh` →
   `02_lxd_add_profile_corr.sh` → `03_lxd_add_corr.sh` → netplan/NTP fixups.
   Every one of those fetches from `http://10.42.0.3/maas/` (see §4).
3. **`install_tarballs`** (`deploy/node-build/`) — installs into `/usr/local`:
   go 1.12.6, thrust 1.8.1, cfitsio 3.47, hwloc 2.1.0, SOFA 20190722,
   fftw 3.3.8 (`--enable-float --enable-shared`), **dedisp**, LabJack LJM.
4. **`create_conda_py38`** (`deploy/node-build/`) — builds the `casa38`
   env (Python 3.8.13) from `dsaenv02.yaml` (also archived here).
   ⚠ Assumes `/home/ubuntu/anaconda3` already exists (installed in step 2).
5. **Clone the repo set**: `mr --trust-all -c .mrconfig_production checkout`
   from `/home/ubuntu/proj/dsa110-shell`. `~/.mrtrust` must list that
   `.mrconfig`. This is the step `install_repos_py38` assumes has happened.
6. **`install_repos_py38`** (`deploy/node-build/`) — pip-installs
   dsa110-pyutils, dsa110-antpos, dsa110-calib, dsa110-meridian-fs and builds
   psrdada-python. ⚠ It only covers 5 repos; the C/CUDA builds
   (psrdada, xGPU, sigproc, mbheimdall) come from the `install =` stanzas in
   `.mrconfig_production`.
7. **`set_sysctl`** (`deploy/node-build/`) — 40 GbE tuning
   (`rmem_max=536870912`, `netdev_max_backlog=250000`, …).
   ⚠ Runtime-only: there is no `/etc/sysctl.d/` drop-in, so this is **lost on
   every reboot** unless re-run.

### The modern (dsart) real-time stack
Steps 3–7 above are the *legacy* py38 path. The current real-time pipeline is
built by **`dsa110-rt/tools/ops/`** (`install_psrdada.sh`, `install_dsart.sh`,
`dsart-rt services install`, `sysctl.sh`) into a **Miniforge3** env
`dsa110-rt` (Python 3.11) — not `casa38`. See the rebuild runbook in the
`dsa110-rt` repo (`docs/DISASTER_RECOVERY.md`).

---

## 3. Deploying / updating code on existing nodes

From `dsa110maas`, in `/home/ubuntu/proj/dsa110-shell`:

```bash
./deploy install <host> <version>     # or: update / current
./deploy_screen.bash                  # one screen per node, pinned version
```

`deploy` SSHes to each host, does `git fetch --tags && git checkout <version>`
in this repo, then runs `mr --trust-all -c .mrconfig_production update` (and
`install`) under conda env `casa38`.

**`.mrconfig_production` is the bill of materials** — every repo pinned to a
tag. Currently deployed set:

| repo | tag | | repo | tag |
| --- | --- | --- | --- | --- |
| dsa110-cnf | v2.0.0 | | dsa110-xengine | v3.1.0-rc116 |
| dsa110-hwmc | v1.0.0-rc1 | | pyuvdata | v101.0.0 |
| dsa110-sigproc | v100.0.0 | | psrdada-python | v100.0.0 |
| dsa110-psrdada | v100.0.0 | | dsa110-pyutils | v3.6.1 |
| dsa110-xGPU | v100.2.0 | | dsa110-calib | v3.0.1 |
| dsa110-T2 | 2.4.2 | | dsa110-meridian-fs | v1.7.0 |
| dsa110-antpos | v1.4.0 | | dsa110-nsfrb | v1.0.3 |
| | | | dsa110-mbheimdall | v1.2.0-rc11 |

⚠ **Stale host naming.** `deploy`'s `all` loop and `check_versions.sh` still use
`corrNN.sas.pvt`, and `deploy_screen.bash` uses `hNN.pro.pvt` — **neither
resolves today**. The `pro.pvt` zone serves `n01`…`n22`. Fix the host lists
before relying on these.

---

## 4. The artifact store — the biggest single point of failure

Every provisioning script fetches from **`http://10.42.0.3/maas/`**, served by
nginx on **`lxd110maas`**. That host:

- is **not** in the MaaS machine/device inventory,
- is **not** in DNS (`lxd110maas.ovro.pvt` does not resolve — the scripts only
  work because they hardcode the literal IP `10.42.0.3`),
- is not covered by any backup or config management.

Two artifacts exist **nowhere else in the world**: `dedisp.tar` and the LabJack
installer. SOFA's upstream is 404. If this host's disk dies, node rebuilds break
silently.

**A full mirror (3.1 GB, 20 artifacts) now lives at**
`/dataz/dsa110/dr_archive/lxd110maas-artifacts/` on h23, with md5s in
`lxd110maas-artifacts.md5`. To rebuild without `lxd110maas`, serve that
directory over HTTP and point `TARBALL_LOC` / `WEB_MAAS` at it.

**Not mirrored deliberately:** `config/lxd/lxd_node.yml` — it carries
credentials, so it is excluded from both the mirror and this repo. Handle it
out of band; see the private notes referenced in §5.

---

## 5. Where the originals live (and what's backed up)

| Item | Original | DR copy |
| --- | --- | --- |
| `install_tarballs` | `http://10.42.0.3/maas/config/lxd/` | here + artifact mirror |
| `create_conda_py38`, `install_repos_py38`, `dsaenv02.yaml` | `dsa110maas:/home/ubuntu/proj/run_on_cluster/` | here + `/dataz/dsa110/dr_archive/run_on_cluster-scripts/` |
| curtin preseeds | `dsa110maas:/etc/maas/preseeds/` | here (**sanitized**) + config tarball |
| MaaS database (25 machines, IPs, DHCP, power creds) | `dsa110maas` postgres `maasdb`, 5.7 GB | `/dataz/dsa110/dr_archive/dsa110maas-config/maasdb-*.pgdump` (1.7 GB) |
| `/etc/maas`, `/etc/bind`, `dhcpd.conf`, `proj/maas` (incl. offline Mellanox driver bundle), `~/bin` | `dsa110maas` | `/dataz/dsa110/dr_archive/dsa110maas-config/*.tar.gz` |

⚠ The DR archive lives on h23's `/dataz` (raidz1, survives one disk). It is
**not off-site**, and it does not protect against loss of h23 itself.

### Credentials — deliberately NOT in git
This repository is public. The archived preseeds here carry
`__UBUNTU_PASSWORD__` and `__LXD_TRUST_PASSWORD__` placeholders, and the
`authorized_keys` bodies in `corrprofile.yaml` are `__REDACTED_PUBKEY__`.
Substitute the real values, which live only on `dsa110maas` and in the
mode-700 DR archive, before using these files.

The inventory of which credential lives where — and the rotation priority
list — is kept out of this repo, in
`/dataz/dsa110/dr_archive/SECRETS_INVENTORY.md` (mode 600) on h23.

---

## 6. Known gaps (as of 2026-07-29)

1. `lxd110maas` unmanaged/undiscoverable — see §4.
2. MaaS postgres had **no backup** before this one; nothing schedules it.
3. `install_tarballs` has no `set -e`, no checksums, no idempotency; a partial
   run leaves half-built trees.
4. `create_conda_py38` pins 2022 build-strings on the `defaults` channel —
   the env may no longer solve.
5. `sysctl` tuning is runtime-only (§2.7).
6. Host naming in `deploy`/`check_versions.sh` is stale (§3).
7. `run_on_cluster` had ~90 operational scripts with only a README committed.
8. `dsa110-shell` on `dsa110maas` sits on branch `ds/dev`; deployed versions are
   reachable via tags (`v3.1.0-rc*`), but the branch heads on GitHub lag.
