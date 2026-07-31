#!/usr/bin/env python3
"""Rebuild the "Antenna Monitor" Grafana dashboard (uid yVY7CUOZk).

Written 2026-07-31. The dashboard shipped inside the grafana-6.2.5 tarball's
2019 `grafana.db` and had drifted badly from the data actually in InfluxDB:

  * "LNA Current" queried fields ``lnacurrentA``/``lnacurrentB`` -- the real
    field names are ``lna_current_a``/``lna_current_b``.
  * "LNA Current" and "FEB Temperature" both filtered on ``"number" =
    '$number'``.  There is no ``number`` tag and no ``$number`` variable; the
    tag is ``ant_num`` and the variable is ``$ant_num``.  Both panels were
    therefore permanently empty.
  * The two Wind Speed panels read ``wxmon``, which has been empty since the
    etcdWx service was lost -- see the h20-services README.

and it was missing motor temperature, drive state and anything at all from the
back-end boxes.

Rather than hand-write panel JSON, this clones the *existing* working panels as
templates and edits them. Grafana 6.2.5 is schemaVersion 18 and predates the
`timeseries`/`stat` panels, so anything hand-rolled risks silently not
rendering; cloning keeps us schema-correct by construction.

Prerequisite: ``/mon/beb`` must be in etcd2db's monmap or ``bebmon`` will not
exist. It was NOT in the container-era config -- added 2026-07-31.

Usage::

    export GRAFANA_AUTH=admin:...        # h23: ~/.dsart/secrets.env
    python3 build_antenna_dashboard.py --out antenna_dashboard.json
    python3 build_antenna_dashboard.py --post --grafana-url http://lxd110h20.pro.pvt:3000
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import urllib.request

UID = "yVY7CUOZk"

# drv_state enum. Source: dsa110-hwmc interfaces/control_panel.py:47, which is
# the operator UI's own mapping for this exact field.
#
# NOTE there is a second, conflicting dict in hwmc/dsa_labjack.py:237 --
# DRIVE_STATE = {0:' Off', 1:'North', 2:'South', 3:'Bad'}.  It is dead code: it
# is defined and never referenced anywhere in the tree, and its shape matches
# the 2-bit drv_cmd/drv_act fields ('halt','north','south','invalid') rather
# than drv_state, which is read from the analog a_values[20]. The values seen in
# the field so far are 0 and 2 -> 'halt' and 'acquired', which is consistent.
DRV_STATE = ["halt", "seek", "acquired", "timeout", "fw_lim_n", "fw_lim_s"]


def fetch_current(url: str, auth: str) -> dict:
    req = urllib.request.Request(f"{url.rstrip('/')}/api/dashboards/uid/{UID}")
    _add_auth(req, auth)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["dashboard"]


def _add_auth(req: urllib.request.Request, auth: str) -> None:
    import base64

    if auth:
        req.add_header(
            "Authorization",
            "Basic " + base64.b64encode(auth.encode()).decode(),
        )


def by_id(panels: list, pid: int) -> dict:
    for p in panels:
        if p.get("id") == pid:
            return p
    raise KeyError(f"panel id {pid} not found in the existing dashboard")


def graph(tpl: dict, *, pid, title, query, x, y, w, h, unit,
          alias=None, stepped=False, ymin=None, ymax=None, fill=None,
          decimals=None) -> dict:
    """Clone a known-good graph panel and retarget it."""
    p = copy.deepcopy(tpl)
    p["id"] = pid
    p["title"] = title
    p["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
    p["targets"] = [{
        "query": query,
        "rawQuery": True,
        "refId": "A",
        "resultFormat": "time_series",
        **({"alias": alias} if alias else {}),
    }]
    p["steppedLine"] = stepped
    if fill is not None:
        p["fill"] = fill
    p["yaxes"] = [
        {"format": unit, "label": None, "logBase": 1,
         "max": ymax, "min": ymin, "show": True,
         **({"decimals": decimals} if decimals is not None else {})},
        {"format": "short", "label": None, "logBase": 1,
         "max": None, "min": None, "show": False},
    ]
    p["legend"] = {"alignAsTable": True, "avg": True, "current": True,
                   "max": True, "min": True, "rightSide": False,
                   "show": True, "total": False, "values": True}
    p["tooltip"] = {"shared": True, "sort": 2, "value_type": "individual"}
    p.pop("thresholds", None)
    p.pop("alert", None)
    return p


def gauge(tpl: dict, *, pid, title, query, x, y, w, h, unit,
          gmin=None, gmax=None, decimals=1) -> dict:
    p = copy.deepcopy(tpl)
    p["id"] = pid
    p["title"] = title
    p["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
    p["targets"] = [{"query": query, "rawQuery": True, "refId": "A",
                     "resultFormat": "time_series"}]
    opts = p.setdefault("options", {})
    fo = opts.setdefault("fieldOptions", {})
    defaults = fo.setdefault("defaults", {})
    defaults["unit"] = unit
    defaults["decimals"] = decimals
    if gmin is not None:
        defaults["min"] = gmin
    if gmax is not None:
        defaults["max"] = gmax
    return p


def state_panel(tpl: dict, *, pid, title, query, x, y, w, h) -> dict:
    """Current drive state for the selected antenna, as a labelled value."""
    p = copy.deepcopy(tpl)
    p["id"] = pid
    p["type"] = "singlestat"
    p["title"] = title
    p["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
    p["targets"] = [{"query": query, "rawQuery": True, "refId": "A",
                     "resultFormat": "time_series"}]
    p["valueName"] = "current"
    p["mappingType"] = 1
    p["mappingTypes"] = [{"name": "value to text", "value": 1},
                         {"name": "range to text", "value": 2}]
    p["valueMaps"] = [{"op": "=", "text": name, "value": str(i)}
                      for i, name in enumerate(DRV_STATE)]
    p["colorBackground"] = True
    p["colorValue"] = False
    # halt/acquired are normal; seek amber; timeout and the limit switches red.
    p["colors"] = ["#299c46", "#e5ac0e", "#d44a3a"]
    p["thresholds"] = "3,4"
    p["sparkline"] = {"fillColor": "rgba(31,118,189,0.18)", "full": False,
                      "lineColor": "rgb(31,120,193)", "show": True}
    p["format"] = "none"
    p["decimals"] = 0
    for k in ("options", "fieldConfig", "yaxes", "xaxis", "legend", "lines",
              "fill", "linewidth", "steppedLine", "seriesOverrides",
              "nullPointMode", "tooltip", "bars", "points", "renderer"):
        p.pop(k, None)
    return p


def row(pid: int, title: str, y: int, collapsed: bool = False,
        panels: list | None = None) -> dict:
    return {"id": pid, "type": "row", "title": title, "collapsed": collapsed,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
            "panels": panels or []}


def build(cur: dict) -> dict:
    old = cur["panels"]
    g_tpl = by_id(old, 2)     # RF Power  -- the good graph template
    gauge_tpl = by_id(old, 12)  # El       -- the good gauge template
    febA = by_id(old, 22)     # fleet-wide FEB A temp gauge (works as-is)
    wind_gauge = by_id(old, 20)
    wind_graph = by_id(old, 18)

    SEL = "\"ant_num\" = '$ant_num'"
    P = []
    pid = 100

    def nid():
        nonlocal pid
        pid += 1
        return pid

    # ---------- fleet overview ----------
    P.append(row(1, "Fleet overview — all antennas", 0))
    f = copy.deepcopy(febA)
    f["gridPos"] = {"x": 0, "y": 1, "w": 24, "h": 4}
    f["title"] = "FEB A temperature — all antennas"
    P.append(f)

    P.append(graph(
        g_tpl, pid=nid(), title="Motor temperature — all antennas",
        query='SELECT mean("motor_temp") FROM "antmon" WHERE $timeFilter '
              'GROUP BY time($__interval),"ant_num" fill(null)',
        alias="$tag_ant_num", x=0, y=5, w=12, h=9, unit="celsius", fill=0))

    P.append(graph(
        g_tpl, pid=nid(),
        title="Drive state — all antennas (0 halt · 1 seek · 2 acquired · "
              "3 timeout · 4 fw_lim_n · 5 fw_lim_s)",
        query='SELECT mean("drv_state") FROM "antmon" WHERE $timeFilter '
              'GROUP BY time($__interval),"ant_num" fill(previous)',
        alias="$tag_ant_num", x=12, y=5, w=12, h=9, unit="short",
        stepped=True, fill=0, ymin=-0.5, ymax=5.5, decimals=0))

    # ---------- pointing, selected antenna ----------
    P.append(row(2, "Antenna $ant_num — pointing & drive", 14))
    P.append(gauge(gauge_tpl, pid=nid(), title="Elevation",
                   query=f'SELECT last("ant_el") FROM "antmon" WHERE {SEL}',
                   x=0, y=15, w=5, h=4, unit="degree"))
    P.append(gauge(gauge_tpl, pid=nid(), title="Commanded elevation",
                   query=f'SELECT last("ant_cmd_el") FROM "antmon" WHERE {SEL}',
                   x=5, y=15, w=5, h=4, unit="degree"))
    P.append(gauge(gauge_tpl, pid=nid(), title="El std dev (30 s)",
                   query=f'SELECT stddev("ant_el") FROM "antmon" WHERE {SEL} '
                         'AND time > now() - 30s',
                   x=10, y=15, w=5, h=4, unit="degree", decimals=3))
    P.append(state_panel(
        gauge_tpl, pid=nid(), title="Drive state",
        query=f'SELECT last("drv_state") FROM "antmon" WHERE {SEL}',
        x=15, y=15, w=5, h=4))
    P.append(gauge(gauge_tpl, pid=nid(), title="Motor temp",
                   query=f'SELECT last("motor_temp") FROM "antmon" WHERE {SEL}',
                   x=20, y=15, w=4, h=4, unit="celsius"))

    P.append(graph(
        g_tpl, pid=nid(), title="Elevation — antenna $ant_num",
        query=f'SELECT mean("ant_el") AS "elevation", '
              f'mean("ant_cmd_el") AS "commanded" FROM "antmon" '
              f'WHERE $timeFilter AND {SEL} GROUP BY time($__interval)',
        x=0, y=19, w=12, h=9, unit="degree", fill=0))
    P.append(graph(
        g_tpl, pid=nid(), title="Elevation error — antenna $ant_num",
        query=f'SELECT mean("ant_el_err") AS "el error" FROM "antmon" '
              f'WHERE $timeFilter AND {SEL} GROUP BY time($__interval)',
        x=12, y=19, w=12, h=9, unit="degree", fill=1))

    # ---------- signal chain, selected antenna ----------
    P.append(row(3, "Antenna $ant_num — signal chain", 28))
    # RF power and BEB IF power are deliberately identical in shape so the two
    # can be read against each other at a glance.
    P.append(graph(
        g_tpl, pid=nid(), title="RF power (front end)",
        query='SELECT mean("rf_pwr_a") as "polA", mean("rf_pwr_b") as "polB" '
              f'FROM "antmon" WHERE $timeFilter AND {SEL} '
              'GROUP BY time($__interval)',
        x=0, y=29, w=12, h=9, unit="dB", fill=0))
    P.append(graph(
        g_tpl, pid=nid(), title="IF power (back-end box)",
        query='SELECT mean("if_pwr_a") as "polA", mean("if_pwr_b") as "polB" '
              f'FROM "bebmon" WHERE $timeFilter AND {SEL} '
              'GROUP BY time($__interval)',
        x=12, y=29, w=12, h=9, unit="dB", fill=0))

    P.append(graph(
        g_tpl, pid=nid(), title="LNA current",
        query='SELECT mean("lna_current_a") as "polA", '
              'mean("lna_current_b") as "polB" FROM "antmon" '
              f'WHERE $timeFilter AND {SEL} GROUP BY time($__interval)',
        x=0, y=38, w=12, h=8, unit="amp", fill=1))
    P.append(graph(
        g_tpl, pid=nid(), title="FEB temperature",
        query='SELECT mean("feb_temp_a") as "A", mean("feb_temp_b") as "B" '
              f'FROM "antmon" WHERE $timeFilter AND {SEL} '
              'GROUP BY time($__interval)',
        x=12, y=38, w=12, h=8, unit="celsius", fill=1))

    # ---------- weather: kept, but honestly labelled ----------
    wg = copy.deepcopy(wind_gauge)
    wg["gridPos"] = {"x": 0, "y": 47, "w": 6, "h": 5}
    wgr = copy.deepcopy(wind_graph)
    wgr["gridPos"] = {"x": 6, "y": 47, "w": 18, "h": 5}
    P.append(row(4, "Weather — EMPTY: needs the etcdWx service, which is not "
                    "deployed (see h20-services README)", 46,
                 collapsed=True, panels=[wg, wgr]))

    d = copy.deepcopy(cur)
    d["panels"] = P
    d["title"] = "Antenna Monitor"
    d["uid"] = UID
    d["refresh"] = "30s"
    d["time"] = {"from": "now-1h", "to": "now"}
    d.pop("id", None)
    return d


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out")
    ap.add_argument("--post", action="store_true")
    ap.add_argument("--grafana-url", default="http://lxd110h20.pro.pvt:3000")
    ap.add_argument("--grafana-auth", default=os.environ.get("GRAFANA_AUTH", ""))
    a = ap.parse_args(argv)

    cur = fetch_current(a.grafana_url, a.grafana_auth)
    dash = build(cur)
    n = len([p for p in dash["panels"] if p.get("type") != "row"])
    print(f"built {len(dash['panels'])} panels ({n} non-row)")

    if a.out:
        with open(a.out, "w") as f:
            json.dump(dash, f, indent=2)
        print(f"wrote {a.out}")

    if a.post:
        body = json.dumps({"dashboard": dash, "overwrite": True,
                           "message": "rebuilt by build_antenna_dashboard.py"}).encode()
        req = urllib.request.Request(
            a.grafana_url.rstrip("/") + "/api/dashboards/db", data=body,
            headers={"Content-Type": "application/json"})
        _add_auth(req, a.grafana_auth)
        with urllib.request.urlopen(req, timeout=60) as r:
            print("posted:", json.load(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
