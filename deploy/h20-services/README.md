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
