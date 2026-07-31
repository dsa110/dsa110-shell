#!/usr/bin/env python3
"""etcd -> InfluxDB bridge. Drop-in replacement for the etcd2db Go binary.

Why this exists
---------------
The prebuilt ``etcd2db`` binary (v1.2.0-dirty, 2020) stops forwarding roughly
every 2-3 minutes under the live load and gives no indication at all: no log
line, no non-zero exit, systemd still reports ``active``. It was diagnosed on
2026-07-31 by running an independent python-etcd3 watcher on the *same* prefix
at the *same* time: the probe stayed alive 270 s continuously at 97 events/s
with sub-second latency while etcd2db needed 5 restarts in 12 minutes. etcd
itself was demonstrably healthy throughout --
``etcd_debugging_mvcc_slow_watcher_total 0`` and ``pending_events_total 0``, so
the server was neither cancelling nor backlogging watches. The fault was the
client.

Contract preserved
------------------
Deliberately reads the **same two config files** as the Go binary, so the
monmap stays the single source of truth and nothing else needs to change:

  * ``etcdConfig.yml``   -> ``endpoints: ["host:port"]``
  * ``etcd2dbConf.yml``  -> ``influxurl``, ``influxdbname``, and
                            ``monmap: {<etcd prefix>: {tablename, tagnames}}``

and produces byte-compatible measurements: keys under each prefix become points
in ``tablename``, the names listed in ``tagnames`` become **tags**, everything
else becomes **fields**, and the payload's ``time`` field (which is **MJD**, not
Unix epoch -- see hwmc ``ANT_MPS = {'time': ("MJD", ...)}``) becomes the point
timestamp.

Field typing matters: InfluxDB rejects a write that changes an existing field's
type. The live schema is float/boolean/string (antmon 46 fields, bebmon 21), so
every number is emitted as a float -- never as an ``i``-suffixed integer, which
would collide with the existing float series.

What it fixes beyond not wedging
--------------------------------
* **Delete events.** The Go binary logged ``unexpected end of JSON input`` on
  every key deletion because it parsed the empty value of a delete event. Here
  deletes are skipped.
* **Watch recovery.** Each prefix watch is supervised; if it throws or goes
  silent past ``--stall-timeout``, it is torn down and re-established, and that
  is logged. This is the specific failure the watchdog existed to paper over.
* **Observability.** Periodic counters (events in, points written, errors,
  reconnects) so "is it actually working" is answerable from the journal.

Batching keeps InfluxDB writes sane at ~100 events/s: points accumulate and
flush on ``--flush-interval`` or ``--batch-size``, whichever comes first.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import etcd3
import yaml

LOG = logging.getLogger("etcd2db_bridge")

#: MJD of the Unix epoch. Payload "time" fields are MJD.
MJD_UNIX_EPOCH = 40587.0


# --------------------------------------------------------------------------
# line protocol
# --------------------------------------------------------------------------

def _esc_tag(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def _esc_meas(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,")


def _esc_str_field(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def to_line(measurement: str, payload: dict, tagnames: list[str]) -> str | None:
    """Render one etcd value as an InfluxDB line-protocol point.

    Returns None when there is nothing writable (no fields, or no usable
    timestamp), rather than emitting a malformed line.
    """
    if not isinstance(payload, dict):
        return None

    tags = []
    fields = []
    for k, v in payload.items():
        if k == "time":
            continue                      # becomes the timestamp, not a field
        if k in tagnames:
            # Tags are always strings in influx; bool/int tags must not vary in
            # representation or they create separate series.
            if v is None:
                continue
            tags.append("%s=%s" % (_esc_tag(k), _esc_tag(v)))
            continue
        if v is None:
            continue
        if isinstance(v, bool):
            fields.append("%s=%s" % (_esc_tag(k), "true" if v else "false"))
        elif isinstance(v, (int, float)):
            f = float(v)
            if f != f or f in (float("inf"), float("-inf")):
                continue                  # influx cannot store NaN/Inf
            # Always float: the live series are float, and an integer literal
            # (`5i`) would be a type conflict.
            fields.append("%s=%s" % (_esc_tag(k), repr(f)))
        elif isinstance(v, str):
            fields.append('%s="%s"' % (_esc_tag(k), _esc_str_field(v)))
        else:
            fields.append('%s="%s"' % (_esc_tag(k), _esc_str_field(json.dumps(v))))

    if not fields:
        return None

    mjd = payload.get("time")
    if not isinstance(mjd, (int, float)) or isinstance(mjd, bool):
        return None
    ts_ns = int((float(mjd) - MJD_UNIX_EPOCH) * 86400.0 * 1e9)
    if ts_ns <= 0:
        return None

    head = _esc_meas(measurement)
    if tags:
        head += "," + ",".join(tags)
    return "%s %s %d" % (head, ",".join(fields), ts_ns)


# --------------------------------------------------------------------------
# influx writer
# --------------------------------------------------------------------------

class InfluxWriter:
    def __init__(self, url: str, db: str, batch_size: int, flush_interval: float):
        self.write_url = url.rstrip("/") + "/write?" + urllib.parse.urlencode(
            {"db": db, "precision": "ns"})
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._buf: list[str] = []
        self._lock = threading.Lock()
        self._last_flush = time.time()
        self.written = 0
        self.errors = 0
        self.dropped = 0

    def add(self, line: str) -> None:
        with self._lock:
            self._buf.append(line)
            due = (len(self._buf) >= self.batch_size
                   or (time.time() - self._last_flush) >= self.flush_interval)
        if due:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            if not self._buf:
                self._last_flush = time.time()
                return
            batch, self._buf = self._buf, []
            self._last_flush = time.time()
        body = "\n".join(batch).encode()
        try:
            req = urllib.request.Request(self.write_url, data=body,
                                         headers={"Content-Type": "text/plain"})
            urllib.request.urlopen(req, timeout=20).read()
            self.written += len(batch)
        except urllib.error.HTTPError as exc:
            self.errors += 1
            detail = ""
            try:
                detail = exc.read().decode("utf8", "replace")[:300]
            except Exception:                                   # noqa: BLE001
                pass
            # A 4xx is a bad point (usually a field type conflict) and will
            # never succeed; dropping it is correct. A 5xx is transient.
            self.dropped += len(batch)
            LOG.error("influx write HTTP %s, dropped %d point(s): %s",
                      exc.code, len(batch), detail)
        except Exception as exc:                                # noqa: BLE001
            self.errors += 1
            self.dropped += len(batch)
            LOG.error("influx write failed, dropped %d point(s): %s",
                      len(batch), exc)


# --------------------------------------------------------------------------
# watch supervision
# --------------------------------------------------------------------------

class PrefixWatcher:
    """One supervised watch on an etcd prefix.

    The Go binary's fatal flaw was that a dead watch stayed dead silently. Here
    every callback bumps ``last_event``; the supervisor re-establishes the watch
    if it throws, or if nothing arrives for ``stall_timeout`` while the prefix
    is expected to be busy.
    """

    def __init__(self, client_factory, prefix: str, measurement: str,
                 tagnames: list[str], writer: InfluxWriter, stall_timeout: float):
        self.client_factory = client_factory
        self.prefix = prefix
        self.measurement = measurement
        self.tagnames = list(tagnames or [])
        self.writer = writer
        self.stall_timeout = stall_timeout
        self.events = 0
        self.skipped = 0
        self.reconnects = 0
        self.last_event = 0.0
        self._client = None
        self._watch_id = None
        self._lock = threading.Lock()

    def _on_event(self, response) -> None:
        try:
            for ev in getattr(response, "events", []) or []:
                # Deletes carry an empty value. The Go binary tried to json
                # parse it and logged "unexpected end of JSON input"; skip.
                if type(ev).__name__.lower().startswith("delete"):
                    continue
                raw = getattr(ev, "value", None)
                if not raw:
                    continue
                with self._lock:
                    self.events += 1
                    self.last_event = time.time()
                try:
                    payload = json.loads(raw.decode("utf8", "replace"))
                except ValueError:
                    self.skipped += 1
                    continue
                line = to_line(self.measurement, payload, self.tagnames)
                if line is None:
                    self.skipped += 1
                    continue
                self.writer.add(line)
        except Exception:                                       # noqa: BLE001
            LOG.exception("callback error on %s", self.prefix)

    def start(self) -> None:
        self.stop()
        self._client = self.client_factory()
        self._watch_id = self._client.add_watch_prefix_callback(
            self.prefix, self._on_event)
        with self._lock:
            self.last_event = time.time()
        LOG.info("watching %s -> %s (tags=%s)",
                 self.prefix, self.measurement, self.tagnames or "none")

    def stop(self) -> None:
        if self._client is not None and self._watch_id is not None:
            try:
                self._client.cancel_watch(self._watch_id)
            except Exception:                                   # noqa: BLE001
                pass
        if self._client is not None:
            # Give the library's watch thread a moment to notice the
            # cancellation before the channel goes away, otherwise it raises
            # "Cannot invoke RPC: Channel closed!" from inside its own thread
            # where we cannot catch it -- pure shutdown noise in the journal.
            time.sleep(0.2)
            try:
                self._client.close()
            except Exception:                                   # noqa: BLE001
                pass
        self._client = None
        self._watch_id = None

    def check(self) -> None:
        """Re-establish the watch if a *previously active* prefix goes quiet.

        The ``self.events > 0`` guard matters. /mon/wx has no publisher at all
        (etcdWx was never recovered) and /mon/cal only sees traffic during a
        calibration, so both are legitimately idle for hours. Without the guard
        the supervisor read that as a stall and re-established those two watches
        every stall_timeout forever -- 6 reconnects each in a 10-minute soak.
        That is not just noise: a prefix reconnecting constantly can never be
        distinguished from one that has genuinely wedged. Idleness is only
        evidence of a problem once we have seen the prefix deliver something.
        """
        with self._lock:
            idle = time.time() - self.last_event
            seen_traffic = self.events > 0
        if seen_traffic and idle > self.stall_timeout:
            self.reconnects += 1
            LOG.warning("%s idle %.0fs (> %.0fs) — re-establishing watch "
                        "(reconnect #%d)", self.prefix, idle,
                        self.stall_timeout, self.reconnects)
            try:
                self.start()
            except Exception as exc:                            # noqa: BLE001
                LOG.error("re-establish on %s failed: %s", self.prefix, exc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--etcd-config", default="etcdConfig.yml")
    ap.add_argument("--conf", default="etcd2dbConf.yml")
    ap.add_argument("--batch-size", type=int, default=400)
    ap.add_argument("--flush-interval", type=float, default=1.0)
    ap.add_argument("--stall-timeout", type=float, default=90.0,
                    help="re-establish a watch after this many idle seconds")
    ap.add_argument("--report-interval", type=float, default=300.0)
    a = ap.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
        stream=sys.stdout)

    with open(a.etcd_config) as f:
        ecfg = yaml.safe_load(f) or {}
    with open(a.conf) as f:
        ccfg = yaml.safe_load(f) or {}

    host, port = str(ecfg["endpoints"][0]).split(":")
    influx_url = ccfg["influxurl"]
    db = ccfg["influxdbname"]
    monmap = ccfg.get("monmap") or {}
    if not monmap:
        LOG.error("monmap is empty in %s — nothing to bridge", a.conf)
        return 1

    LOG.info("etcd2db_bridge starting: etcd=%s:%s influx=%s db=%s prefixes=%d",
             host, port, influx_url, db, len(monmap))

    writer = InfluxWriter(influx_url, db, a.batch_size, a.flush_interval)

    def client_factory():
        return etcd3.client(host=host, port=int(port))

    watchers = []
    for prefix, spec in monmap.items():
        spec = spec or {}
        w = PrefixWatcher(client_factory, str(prefix),
                          str(spec.get("tablename") or str(prefix).strip("/").replace("/", "_")),
                          spec.get("tagnames") or [], writer, a.stall_timeout)
        watchers.append(w)

    stopping = threading.Event()

    def _sig(_signo, _frame):
        LOG.info("signal received, shutting down")
        stopping.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    for w in watchers:
        try:
            w.start()
        except Exception as exc:                                # noqa: BLE001
            LOG.error("initial watch on %s failed: %s (supervisor will retry)",
                      w.prefix, exc)

    last_report = time.time()
    try:
        while not stopping.is_set():
            stopping.wait(5.0)
            if stopping.is_set():
                break
            writer.flush()
            for w in watchers:
                w.check()
            if time.time() - last_report >= a.report_interval:
                last_report = time.time()
                LOG.info("written=%d errors=%d dropped=%d | %s",
                         writer.written, writer.errors, writer.dropped,
                         " ".join("%s:ev=%d,skip=%d,rc=%d"
                                  % (w.measurement, w.events, w.skipped, w.reconnects)
                                  for w in watchers))
    finally:
        writer.flush()
        for w in watchers:
            w.stop()
        LOG.info("stopped: written=%d errors=%d dropped=%d",
                 writer.written, writer.errors, writer.dropped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
