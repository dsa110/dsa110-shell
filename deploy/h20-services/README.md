# lxd110h20 monitoring/control services — bare-metal deployment

The five services that make up the DSA-110 monitoring and control plane, as
systemd units on bare metal:

| Unit | Port | What it is |
| --- | --- | --- |
| `etcdv3.service` | 2379 | etcd v3 — **the control plane**. Reached fleet-wide as `etcdv3service.pro.pvt:2379`. |
| `influxdb.service` | 8086 | InfluxDB 1.7.7, database `dsa110`. |
| `etcd2db.service` | – | Bridges etcd `/mon/{ant,cal,wx}` → InfluxDB `{antmon,calmon,wxmon}`. |
| `grafana.service` | 3000 | Grafana 6.2.5 dashboards. |
| `webserverUI.service` | 9090 | `websrv` — the DSA-110 web UI. |

Written 2026-07-30 while promoting `lxd110h24` to be the new `lxd110h20`, after
the 2026-07-27 power event destroyed the original h20's NVMe. Everything here
was verified against the running host, not copied from older docs.

---

## 1. Where the pieces come from

These services were originally one-LXD-container-each, then moved to bare metal
on h20. The install scripts (`install_etcdv3`, `install_influxdb`,
`install_grafana`, `install_webserverUI`) all fetch from
`https://ssh.ovro.caltech.edu/~rh/maas/`, **which no longer serves** (HTTP 000).
The tarballs must come from a local mirror.

**Nothing here compiles.** Every binary ships prebuilt:

| Artifact | Contains | Installs to |
| --- | --- | --- |
| `etcd-io-etcd.tar.gz` | prebuilt `bin/etcd`, `bin/etcdctl` (3.3.0+git) | `~/proj/godev/src/github.com/etcd-io/etcd/` |
| `influxdb-1.7.7-1.tar.gz` | `influxd` + `influxdb.conf` | `~/proj/influxdb-1.7.7-1/` |
| `grafana-6.2.5.tar.gz` | `bin/grafana-server` **and a populated `data/grafana.db`** | `~/proj/grafana-6.2.5/` |
| `websrv.tar.gz` | prebuilt `websrv` (24 MB) + `websrvConfig.yml` | `~/proj/websrv/` |
| `webauth.tar.gz` | `dsa110AuthDb` (32 KB SQLite; 437 B compressed — *not* a placeholder) | `~/webauth/` |
| `etcd2db` | prebuilt Go binary | `~/proj/etcd2db/` |

go 1.12.6 is therefore **optional**. Install it once via `/etc/profile.d/go.sh`
rather than letting each `install_*` script append the same six exports to
`~/.bashrc` (running all four leaves 24 duplicate lines).

⚠ **The Grafana tarball ships a 2019 `grafana.db`** carrying the admin user, 11
dashboards (`DSA110-WX`, `Antenna Monitor`, `DSA119`, `Etcd`, `calibration`, …)
and three datasources. Two consequences: the legacy dashboards survive a total
host loss, and a fresh install does **not** start at `admin/admin`.

## 1a. InfluxDB data lives on the ZFS pool

h20 has its own **6-disk raidz1 pool named `dataz`** (~59 T, distinct from h23's
same-named pool — different GUID, different disks; verify with
`zpool get guid dataz` before ever assuming otherwise). InfluxDB's data was moved
onto it 2026-07-30:

```bash
zfs create -o compression=lz4 -o atime=off dataz/influxdb   # -> /dataz/influxdb
# influxdb.conf: [meta] dir, [data] dir and [data] wal-dir all point at
#                /dataz/influxdb/{meta,data,wal}
chown -R ubuntu:ubuntu /dataz/influxdb
```

`compression=lz4` is measurably worthwhile here — 3.3× on the initial data.
`atime=off` avoids pointless metadata writes.

⚠ **`influxdb.service` carries `RequiresMountsFor=/dataz/influxdb`, and that line
is load-bearing.** If the pool ever fails to import, `/dataz` is just an empty
directory on the root disk — influxd would cheerfully create fresh
`meta/data/wal` there and come up with an **empty database**, silently losing
every dashboard's history until someone noticed. Verified by unmounting the
dataset and starting the unit: systemd holds it in `activating` and **no stray
database is created**. Do not remove that line.

The pool auto-imports at boot via `zfs-import-cache.service` (enabled) using
`/etc/zfs/zpool.cache`; `zfs-mount.service` is also enabled.

## 2. Install order

```bash
# 1. lay out the trees (see table above), then:
install -m 755 bin/start*            /home/ubuntu/bin/
install -m 644 systemd/*.service     /etc/systemd/system/
install -m 644 config/etcdConfig.yml    /home/ubuntu/proj/etcd2db/
install -m 644 config/etcd2dbConf.yml   /home/ubuntu/proj/etcd2db/
install -m 644 config/websrv-etcdConfig.yml /home/ubuntu/proj/websrv/etcdConfig.yml

# 2. influx data dirs must be writable by ubuntu (units run as User=ubuntu)
mkdir -p /var/lib/influxdb/{meta,data,wal} && chown -R ubuntu:ubuntu /var/lib/influxdb

systemctl daemon-reload
systemctl enable --now etcdv3.service influxdb.service
curl -XPOST 'http://127.0.0.1:8086/query' --data-urlencode 'q=CREATE DATABASE "dsa110"'
systemctl enable --now etcd2db.service grafana.service webserverUI.service
```

Create the `dsa110` database **before** starting etcd2db, or it logs write
errors until the database exists.

## 3. What was wrong with the container-era scripts

Fixed in the copies here. Each is a real failure, not a style preference:

1. **`startEtcdServer` could not start at all.** It derived its listen address
   from ``IP=`ifconfig | grep inet | grep broadcast | awk '{print $2}'` ``. That
   yields one address in a single-NIC container but **three** on bare metal
   (br1/pro, br2/sas, docker0), producing an unusable
   `--listen-client-urls`. Now pinned to br1, plus loopback so local clients
   need no DNS.
2. **All five scripts ended with**
   `if [[ "$?" == 0 ]]; then while true; do sleep 60; done; fi`.
   If the daemon exited *cleanly*, the wrapper slept forever — systemd reported
   the unit `active` while the service was dead, and `Restart=on-failure` could
   never fire. Replaced with `exec`.
3. **`ExecStop=/usr/bin/pkill -9 -f startX` was a no-op.** It matched the
   wrapper by name, but the wrapper now `exec`s the daemon, so that name is gone
   from the process table. Removed; systemd's control-group kill is correct.
4. **`startInfluxdb` ignored its own config.** It ran `influxd` with no
   `-config`, so the shipped `influxdb.conf` was never read. Now passed
   explicitly.
5. **`etcd2db.service` had no service ordering** — only
   `After=network-online.target`. On a cold boot it could start before etcd or
   influxd was listening and then sit out `RestartSec=30` repeatedly. Both
   dependencies are now declared (`Wants=`, not `Requires=`, so influx
   maintenance does not forcibly stop the bridge).
6. **Bare hostnames in every config.** `etcdConfig.yml` said
   `endpoints: ["etcdv3service:2379"]` and `etcd2dbConf.yml` said
   `influxurl: "http://influxdbservice:8086"`. `/etc/resolv.conf` has
   `search sas.pvt`, so those expanded to `*.sas.pvt` — names that **cannot
   exist**, because MaaS's `sas.pvt` domain is `authoritative=False` and BIND
   does not serve the zone. Both now use loopback (same host anyway).

## 4. DNS — read this before moving these services

MaaS's **default** domain here is `sas.pvt`, which is `authoritative=False` and
is **not served by BIND at all**. Every machine MaaS commissions therefore gets
a name that can never resolve, and all operational names have to be
hand-created `pro.pvt` dnsresources instead.

Those aliases were originally bound to `alloc_type: Observed` (DHCP-discovered)
addresses on a specific machine interface. That is why `etcdv3service.pro.pvt`,
`influxdbservice.pro.pvt`, `grafanaservice.pro.pvt` and
`webserverUIservice.pro.pvt` all went **NXDOMAIN** the moment the old h20 left
`Deployed` state — the alias itself evaporated, not just its target.

They are now **CNAMEs to `lxd110h20.pro.pvt`**, which is a single literal A
record. One record to change if the host moves, and rescue mode / reboots /
redeploys no longer delete the names.

```bash
maas $P dnsresources create fqdn=lxd110h20.pro.pvt ip_addresses=<ip>
maas $P dnsresource-records create fqdn=etcdv3service.pro.pvt \
        rrtype=CNAME rrdata=lxd110h20.pro.pvt.
```

⚠ MaaS refuses `interface link-subnet` on a **Deployed** machine ("Cannot link
subnet interface because the machine is not Ready, Allocated, or Broken"), so a
static address cannot be assigned without releasing — i.e. without wiping the
OS. Every machine in this installation is consequently `mode: dhcp`. Pin the
address with a **DHCP snippet** if you need it fixed.

## 5. Known issues

- **`websrvConfig.yml` sets `allow-ip: "lxd110maas.ovro.pvt"`**, a name that
  resolves nowhere in this installation (`lxd110maas` is absent from both MaaS
  and DNS; it is only ever reached as the literal `10.42.0.3`). websrv starts
  and serves anyway, but treat its access control as unverified.
- **`etcd2db` SIGSEGVs if etcd is not yet listening when it starts.** It logs
  `etcdaccess init(): Failed to get clientv3`, keeps going, and then panics on a
  nil pointer inside `etcdaccess.Watch()` (`etcdaccess.go:126`, from
  `etcd2db.go:482`). Declaring `After=etcdv3.service` is **not** enough: both
  units are `Type=simple`, so systemd marks etcd active the instant it forks,
  ~1 s before it listens (influxd needs ~10 s more). Observed on the 2026-07-30
  reboot — the unit panicked and only recovered via `Restart=on-failure` 30 s
  later. `startEtcd2db` therefore waits for 127.0.0.1:2379 **and** :8086 to
  accept a connection before exec'ing. Confirmed `NRestarts=0` afterwards.
  (Note the same `etcdaccess` nil-panic signature was what crash-looped
  `etcdWx`.)
- **`etcd2db` mishandles key deletion** — deleting a watched `/mon/*` key makes
  it log `unexpected end of JSON input`, because it tries to parse the empty
  value of the delete event. Harmless but noisy.
- **Mon-point `time` fields are MJD**, not Unix epoch (`ANT_MPS = {'time':
  ("MJD", …)}`). Feeding seconds-since-epoch makes etcd2db reject the point
  with `time outside range …`.
- **Grafana 6.x locks an account after 5 failed logins** ("Login for user
  temporarily blocked"), and then returns 401 even for the *correct* password —
  which looks exactly like a failed password reset. Clear it with
  `delete from login_attempt;` in `grafana.db`.
- **InfluxDB 1.7.7 binds `0.0.0.0:8086` with no authentication**, so it is
  reachable from the sas fabric. This matches how it ran on the old h20;
  restricting the bind address or enabling auth is worth doing.
- **`etcdWx`** (weather: `wx.ovro.pvt/latest.php` → etcd `/mon/wx`) also ran on
  h20 and is **not** in this set — no binary or unit for it survives in the
  staged artifacts or on the upstream server. Without it, `wxmon` stays empty.
- These are 2019-era builds (etcd 3.3, InfluxDB 1.7.7, Grafana 6.2.5) with known
  CVEs. Keep them off any routable network.

### etcd fills its quota in hours without auto-compaction (2026-07-31)

etcd defaults to **no auto compaction**, so every update to a `/mon` key keeps
its old revision forever. hwmc rewrites ~217 `/mon/{ant,beb}` points at about
**217 revisions/s**, and the backend grows **~11.7 MiB/min (~700 MiB per hour of
retained history)**. Roughly 18 hours after this host was built the backend hit
the 2 GiB default quota, etcd raised a `NOSPACE` alarm and went **read-only**,
and every writer in the observatory began failing with

```
grpc StatusCode.RESOURCE_EXHAUSTED
details = "etcdserver: mvcc: database space exceeded"
```

`antmc.service` on antservice.ant.pvt was the visible casualty. Live data at the
time was **0.16 MiB across 219 keys** — the other 2 GiB was pure history.

`startEtcdServer` now sets `--auto-compaction-mode periodic
--auto-compaction-retention 30m` and `--quota-backend-bytes 8589934592` (8 GiB).

Recovering a wedged instance:

```bash
etcdctl compact $(etcdctl endpoint status -w json | jq '.[0].Status.header.revision')
etcdctl --command-timeout=300s defrag     # 5s default timeout is NOT enough
etcdctl alarm disarm
```

Two traps: compaction is **asynchronous**, so a defrag issued immediately after
it reclaims almost nothing (first pass took 2027 → 695 MiB; a second pass, once
compaction had finished, took it to **0.2 MiB**). And compaction only frees
space *logically* — the bbolt file never shrinks by itself, so it plateaus at
its high-water mark and only `defrag` returns space to the disk.

### etcd2db watchdog

Because of the silent-death mode below, `etcd2db-watchdog.timer` runs
`bin/etcd2db_watchdog` every 2 minutes. It judges liveness on **data** — has
InfluxDB received any `antmon` points in the last 180 s — not on systemd state,
which is always `active` even when the bridge is wedged.

It also distinguishes a wedged bridge from an upstream outage: if etcd has no
fresh `/mon/ant` keys either, the fault is hwmc's and restarting etcd2db would be
pointless churn, so it does nothing and says so. Install:

```bash
install -m 755 bin/etcd2db_watchdog /home/ubuntu/bin/
install -m 644 systemd/etcd2db-watchdog.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now etcd2db-watchdog.timer
journalctl -t etcd2db-watchdog          # its decisions
```

### etcd2db dies silently when its watch breaks (2026-07-31)

After an etcd restart — or any compaction past the revision it is waiting on —
etcd2db **loses its watch and never re-establishes it**. It does not exit, does
not log anything, and keeps reporting `active`; it simply stops forwarding, and
its RSS climbs (1085 MiB observed, versus 17 MiB when healthy). InfluxDB just
stops receiving points, with no error anywhere.

**Always restart `etcd2db` after restarting `etcdv3`.** Check liveness with
data, not with systemd:

```bash
influx -database dsa110 -execute 'SELECT count(*) FROM antmon WHERE time > now() - 5m'
```

### The host clock feeding all antenna data was 8 minutes fast (2026-07-31)

`antservice` and `dsa110maas` are both LXC containers on the bare-metal host at
**10.42.0.3**, and containers cannot set the clock (`CapEff: 0`), so they
inherit the host's. That host had **one** NTP server configured,
`192.168.23.31`, which it has **no route to** — it has no default route, only
the directly-connected 10.40/10.41/10.42 subnets. `ntpq` showed the peer as
`.INIT.` with `reach 0`: never contacted. The clock free-ran for 5 days and
drifted **+494 s (~99 s/day)**.

Because hwmc timestamps every monitor point from that clock, all antenna and BEB
data landed in InfluxDB ~8 minutes **in the future**. InfluxDB 1.x implicitly
caps `GROUP BY time()` queries at `now()`, so future-stamped points are silently
dropped — panels looked broken or simply lagged by 8 minutes.

Fixed by pointing it at three reachable stratum-2 peers on its own subnet
(10.42.0.232 / .200 / .199, all chained to the site PPS clock) and stepping with
`ntpd -gq` (`time set -493.927490 s`), then `hwclock -w`.

⚠ Do **not** simply restore `192.168.23.31`: on hosts that *can* reach it, ntpd
flags it as a **falseticker** (`x` in `ntpq -pn`, ~-619 s). And never configure
a single NTP source — ntpd needs at least three to outvote a bad one.

### The host does not reliably boot unattended (2026-07-30)

This machine is **legacy BIOS, not UEFI** (`/sys/firmware/efi` absent, so
`efibootmgr` cannot help). Root is `/dev/nvme0n1p2`, and the NVMe has a proper
1 MB `BIOS boot` partition with GRUB installed to it — GRUB is correct.

The problem is the other six disks. They are GPT ZFS members, so each carries a
**protective MBR**: a valid `0x55AA` signature at offset 510 but **zero
bootloader code** (`first4=00000000`). A legacy BIOS that picks one of them
loads its MBR, finds nothing to execute, and **hangs** rather than falling
through to the next device. That is why the host needed a manual **F11** disk
selection to come up, and it is why it will *not* return unattended after a
power event.

Mitigation applied, via the Supermicro BMC:

```bash
ipmitool chassis bootdev disk options=persistent
# -> Boot Flag Valid / Options apply to all future boots
#    Boot Device Selector : Force Boot from default Hard-Drive
```

⚠ That forces "default Hard-Drive", which resolves through the BIOS's *own*
internal HDD priority list — so it is only a real fix if that list already puts
the NVMe first. **The durable fix is to set the NVMe first in the BIOS hard-disk
boot order** (BIOS setup, or the BMC web UI's boot-order page), then prove it
with one unattended reboot. Do not zero the spinners' `0x55AA` bytes to make
them "unbootable": that signature is part of GPT's protective MBR and they hold
a live ZFS pool.

Also note MaaS drives this machine's power over IPMI and sets the boot device
itself when commissioning/deploying, so it can overwrite the override above.
