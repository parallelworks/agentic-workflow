#!/usr/bin/env python3
"""render.py — the fractal app's driver: an N x N Mandelbrot canvas rendered
incrementally, tile by tile, across a Ray cluster (a bag of tasks).

A CANVAS is a directory (on the Ray head host: ~/pw-fractal/<canvas-id>/):
    canvas.json    the spec (tiles per side, tile_px, max_iter, palette, region)
    tiles.jsonl    the ledger: one line per finished tile (who rendered it, checksum)
    previews/      the small PNG each finished tile showed on the dashboard
    reserved/      tiles claimed by submissions still running (one file each)
    lock           flock that serializes tile selection between submissions

Subcommands (each prints ONE JSON line last, for agents and workflows):
    init    create the canvas and show it EMPTY on the dashboard
    add     render more tiles (a count, all remaining, a rectangle, or a list)
    status  progress, per-site counts, and the canvas checksum once complete
    redraw  re-post the canvas and every finished tile (after a dashboard restart)

Backends: `--backend ray` (default) runs one actor per Ray worker node, each with
a process pool over the node's cores (the ray-cluster workers start with
--num-cpus=1, so a node has ONE Ray slot and the parallelism inside a node is
ours). `--backend local` renders with a local process pool, for tests.

Exit codes: 0 ok, 1 some tiles failed, 2 usage/canvas error.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import random
import socket
import sys
import time
import urllib.request
import zlib
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import fractal_core as core  # noqa: E402

MAX_TILES = 256           # per side: 65,536 tiles
MAX_TILE_PX = 4096
MAX_ITER = 1_000_000
STALE_RESERVATION_S = 600  # a reservation whose submission stopped heartbeating
HEARTBEAT_S = 30
SCAN_S = 10                # look for newly joined Ray nodes this often
SITE_REFRESH_S = 10
ORDERS = ("row", "random", "center", "spiral")


class UsageError(Exception):
    pass


def emit(obj):
    print(json.dumps(obj, separators=(",", ":"), sort_keys=False), flush=True)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------
# Canvas directory and ledger
# --------------------------------------------------------------------------
class Canvas:
    def __init__(self, path):
        self.dir = os.path.abspath(os.path.expanduser(path))
        self.spec_path = os.path.join(self.dir, "canvas.json")
        self.ledger = os.path.join(self.dir, "tiles.jsonl")
        self.previews = os.path.join(self.dir, "previews")
        self.reserved_dir = os.path.join(self.dir, "reserved")
        self.lock_path = os.path.join(self.dir, "lock")
        self._spec = None

    @property
    def id(self):
        return os.path.basename(self.dir)

    def create(self, spec):
        parent = os.path.dirname(self.dir)
        os.makedirs(parent, exist_ok=True)
        try:
            os.mkdir(self.dir)  # strict claim: an existing canvas is never reused
        except FileExistsError:
            raise UsageError("canvas %s already exists; pick a new canvas id" % self.dir)
        os.mkdir(self.previews)
        os.mkdir(self.reserved_dir)
        open(self.ledger, "a").close()
        with open(self.spec_path, "w") as fh:
            json.dump(spec, fh, indent=2)
            fh.write("\n")
        self._spec = spec

    def spec(self):
        if self._spec is None:
            try:
                with open(self.spec_path) as fh:
                    self._spec = json.load(fh)
            except FileNotFoundError:
                raise UsageError("no canvas at %s (run `render.py init` first)" % self.dir)
        return self._spec

    def save_spec(self):
        tmp = self.spec_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self._spec, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, self.spec_path)

    @contextlib.contextmanager
    def locked(self):
        with open(self.lock_path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def done(self):
        """{(x, y): ledger entry}; the first entry for a tile wins."""
        out = {}
        try:
            with open(self.ledger) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue  # a torn last line from a killed writer
                    out.setdefault((e["x"], e["y"]), e)
        except FileNotFoundError:
            pass
        return out

    def reserved(self, exclude=None):
        """Tiles claimed by other live submissions (stale claims are ignored)."""
        out = set()
        cutoff = time.time() - STALE_RESERVATION_S
        try:
            names = os.listdir(self.reserved_dir)
        except FileNotFoundError:
            return out
        for name in names:
            if not name.endswith(".json") or name[:-5] == exclude:
                continue
            path = os.path.join(self.reserved_dir, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    continue
                with open(path) as fh:
                    out.update(tuple(t) for t in json.load(fh)["tiles"])
            except (OSError, ValueError, KeyError):
                continue
        return out

    def _res_path(self, submission):
        return os.path.join(self.reserved_dir, submission + ".json")

    def reserve(self, submission, tiles):
        with open(self._res_path(submission), "w") as fh:
            json.dump({"submission": submission, "host": socket.gethostname(), "pid": os.getpid(),
                       "started": now_iso(), "tiles": [list(t) for t in tiles]}, fh)

    def heartbeat(self, submission):
        with contextlib.suppress(OSError):
            os.utime(self._res_path(submission))

    def release(self, submission):
        with contextlib.suppress(OSError):
            os.remove(self._res_path(submission))

    def record(self, entry, png_b64):
        with open(os.path.join(self.previews, "%d_%d.png" % (entry["x"], entry["y"])), "wb") as fh:
            fh.write(__import__("base64").b64decode(png_b64))
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        with self.locked():
            with open(self.ledger, "a") as fh:
                fh.write(line)

    def preview_b64(self, x, y):
        try:
            with open(os.path.join(self.previews, "%d_%d.png" % (x, y)), "rb") as fh:
                return __import__("base64").b64encode(fh.read()).decode("ascii")
        except OSError:
            return ""


def canvas_checksum(spec, done):
    """sha256 over every tile checksum in row order; only meaningful when complete."""
    n = spec["tiles"]
    h = hashlib.sha256()
    for y in range(n):
        for x in range(n):
            h.update(done[(x, y)]["checksum"].encode())
            h.update(b"\n")
    return h.hexdigest()


def progress(spec, done, reserved=None):
    total = spec["tiles"] ** 2
    out = {"done": len(done), "total": total, "complete": len(done) >= total}
    if reserved is not None:
        out["reserved"] = len(reserved)
    return out


# --------------------------------------------------------------------------
# Tile selection
# --------------------------------------------------------------------------
def parse_tile(text, n):
    try:
        x, y = (int(v) for v in text.split(","))
    except ValueError:
        raise UsageError("tile %r is not x,y" % text)
    if not (0 <= x < n and 0 <= y < n):
        raise UsageError("tile %r is outside the %dx%d canvas" % (text, n, n))
    return (x, y)


def parse_rect(text, n):
    try:
        x0, y0, x1, y1 = (int(v) for v in text.split(","))
    except ValueError:
        raise UsageError("rect %r is not x0,y0,x1,y1" % text)
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    if x0 < 0 or y0 < 0 or x1 >= n or y1 >= n:
        raise UsageError("rect %r is outside the %dx%d canvas" % (text, n, n))
    return [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


def order_tiles(tiles, order, n, seed):
    c = (n - 1) / 2.0
    if order == "row":
        return sorted(tiles, key=lambda t: (t[1], t[0]))
    if order == "random":
        tiles = sorted(tiles, key=lambda t: (t[1], t[0]))
        random.Random(seed).shuffle(tiles)
        return tiles
    if order == "center":
        return sorted(tiles, key=lambda t: ((t[0] - c) ** 2 + (t[1] - c) ** 2, t[1], t[0]))
    if order == "spiral":  # ring by ring outward, clockwise within a ring
        return sorted(tiles, key=lambda t: (max(abs(t[0] - c), abs(t[1] - c)),
                                            math.atan2(t[1] - c, t[0] - c), t[1], t[0]))
    raise UsageError("order must be one of %s" % ", ".join(ORDERS))


def select_tiles(spec, taken, args, seed):
    n = spec["tiles"]
    if args.tile:
        cand = [parse_tile(t, n) for t in args.tile]
    elif args.rect:
        cand = parse_rect(args.rect, n)
    else:
        cand = [(x, y) for y in range(n) for x in range(n)]
    seen, free = set(), []
    for t in cand:
        if t not in taken and t not in seen:
            seen.add(t)
            free.append(t)
    free = order_tiles(free, args.order, n, seed)
    if args.count is not None:
        free = free[:args.count]
    return free


# --------------------------------------------------------------------------
# Dashboard (the ray-cluster dashboard's HTTP API)
# --------------------------------------------------------------------------
class Dashboard:
    def __init__(self, url):
        self.url = url.rstrip("/") if url else None
        self.failures = 0
        self.last_error = None

    def post(self, path, data):
        if not self.url:
            return False
        req = urllib.request.Request(self.url + path, data=json.dumps(data).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
            return True
        except Exception as e:  # the dashboard is a view; never fail a render over it
            self.failures += 1
            self.last_error = "%s: %s" % (path, e)
            return False

    def get(self, path):
        if not self.url:
            return None
        try:
            with urllib.request.urlopen(self.url + path, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            self.last_error = "%s: %s" % (path, e)
            return None

    def site_metadata(self):
        """site_id -> {cluster_name, scheduler_type}, from the dashboard's worker
        registrations (lost on /api/config, so init saves it in canvas.json)."""
        state = self.get("/api/state") or {}
        meta = {}
        for sid, st in (state.get("site_stats") or {}).items():
            if isinstance(st, dict) and st.get("cluster_name"):
                meta[sid] = {"cluster_name": st.get("cluster_name", ""),
                             "scheduler_type": st.get("scheduler_type", "")}
        for _ip, nd in (state.get("nodes") or {}).items():
            if isinstance(nd, dict) and nd.get("site_id") and nd.get("cluster_name"):
                meta.setdefault(nd["site_id"], {"cluster_name": nd["cluster_name"],
                                                "scheduler_type": nd.get("scheduler_type", "")})
        return meta

    def show_canvas(self, spec):
        n = spec["tiles"]
        self.post("/api/config", {"workload_type": "fractal", "grid_size": n,
                                  "image_size": spec["preview_px"], "total_tiles": n * n})


def site_of(node_ip):
    """The ray-cluster site convention: remote workers join over SSH tunnels on
    127.0.X.Y (site-X; 127.0.0.X is the legacy single-node form); a real IP is a
    worker on the head's own site (site-1)."""
    if node_ip and node_ip.startswith("127."):
        parts = node_ip.split(".")
        try:
            return "site-%d" % int(parts[3] if parts[2] == "0" else parts[2])
        except (IndexError, ValueError):
            pass
    if node_ip == "local":
        return "local"
    return "site-1"


# --------------------------------------------------------------------------
# Backends: each calls on_result(result, node) / on_error(tile, message)
# --------------------------------------------------------------------------
def run_local(spec, tiles, args, on_result, on_error, tick):
    from concurrent.futures import ProcessPoolExecutor, as_completed
    procs = args.processes or len(os.sched_getaffinity(0))
    node = {"ip": "local", "host": socket.gethostname(), "cores": procs}
    with ProcessPoolExecutor(procs) as ex:
        futs = {ex.submit(core.render_tile, spec, x, y): (x, y) for x, y in tiles}
        for f in as_completed(futs):
            try:
                on_result(f.result(), node)
            except Exception as e:
                on_error(futs[f], "%s: %s" % (type(e).__name__, e))
            tick()
    return {"nodes": 1, "cores": procs, "head_ip": None}


def run_ray(spec, tiles, args, on_result, on_error, tick):
    import ray
    from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

    address = args.address or os.environ.get("RAY_ADDRESS") or "auto"
    # No runtime_env: inside a Ray Job, ray.init merges it with the job's own and
    # cannot copy a module object. Each actor gets fractal_core's SOURCE instead.
    ray.init(address=address, log_to_driver=False)
    with open(core.__file__, "rb") as fh:
        core_src = fh.read()
    core_sha = hashlib.sha256(core_src).hexdigest()

    @ray.remote(num_cpus=1)
    class TileWorker:
        """One per worker node: holds the node's single Ray slot and fans tiles
        out to a process pool over the node's cores."""

        # Everything is imported inside the methods: when render.py runs as a script
        # this class is pickled BY VALUE, and a closure over the `ray` module (or any
        # module-level name) cannot be pickled.
        def __init__(self, cores, start_method, core_src, core_sha):
            import multiprocessing
            import os as _os
            import socket as _socket
            import sys as _sys
            import tempfile
            from concurrent.futures import ProcessPoolExecutor
            import ray as _ray
            # Install the driver's fractal_core.py in a node-local dir named by its hash
            # and put that dir on sys.path; the pool's spawned processes inherit sys.path
            # and import it by name. Worker nodes need only Ray and NumPy, not this repo.
            d = _os.path.join(tempfile.gettempdir(), "pw-fractal-core-%s" % core_sha[:16])
            _os.makedirs(d, exist_ok=True)
            path = _os.path.join(d, "fractal_core.py")
            try:
                with open(path, "rb") as fh:
                    current = fh.read() == core_src
            except OSError:
                current = False
            if not current:
                tmp = "%s.%d.tmp" % (path, _os.getpid())
                with open(tmp, "wb") as fh:
                    fh.write(core_src)
                _os.replace(tmp, path)
            if d not in _sys.path:
                _sys.path.insert(0, d)
            _os.environ["PYTHONPATH"] = d + _os.pathsep + _os.environ.get("PYTHONPATH", "")
            import fractal_core  # noqa: F401
            self.cores = cores or len(_os.sched_getaffinity(0))
            self.pool = ProcessPoolExecutor(self.cores,
                                            mp_context=multiprocessing.get_context(start_method))
            self.ip = _ray.util.get_node_ip_address()
            self.host = _socket.gethostname()

        def info(self):
            return {"cores": self.cores, "ip": self.ip, "host": self.host}

        def render(self, spec, x, y):
            import fractal_core
            return self.pool.submit(fractal_core.render_tile, spec, x, y).result()

    head_ip = os.environ.get("RAY_HEAD_IP")
    if not head_ip:
        for n in ray.nodes():
            if n.get("Alive") and "node:__internal_head__" in (n.get("Resources") or {}):
                head_ip = n.get("NodeManagerAddress")
    pending = deque(tiles)
    retries = {}
    actors = {}   # node_id -> dict(handle, cores, ip, host, inflight, ready)
    refs = {}     # ObjectRef -> (kind, node_id, tile)
    seen_ready = {}
    idle_since = time.time()

    def scan():
        for n in ray.nodes():
            nid = n.get("NodeID")
            if (not n.get("Alive") or nid in actors or nid in dropped
                    or (n.get("Resources") or {}).get("CPU", 0) < 1):
                continue
            h = TileWorker.options(
                scheduling_strategy=NodeAffinitySchedulingStrategy(nid, soft=False),
                max_concurrency=1024).remote(args.cores_per_node, args.pool_start, core_src, core_sha)
            actors[nid] = {"handle": h, "cores": 0, "ip": n.get("NodeManagerAddress"), "host": "",
                           "inflight": 0, "ready": False}
            refs[h.info.remote()] = ("info", nid, None)

    def drop(nid, why):
        a = actors.pop(nid, None)
        dropped.add(nid)
        if a:
            print("node %s dropped: %s" % (a["ip"], why), flush=True)
            with contextlib.suppress(Exception):
                ray.kill(a["handle"])

    dropped = set()
    scan()
    last_scan = time.time()
    try:
        while pending or any(a["inflight"] for a in actors.values()):
            for nid, a in list(actors.items()):
                while a["ready"] and pending and a["inflight"] < 2 * a["cores"]:
                    t = pending.popleft()
                    refs[a["handle"].render.remote(spec, t[0], t[1])] = ("tile", nid, t)
                    a["inflight"] += 1
            if any(a["ready"] for a in actors.values()):
                idle_since = time.time()
            elif pending and time.time() - idle_since > args.node_wait:
                # no ready worker node (none joined, all dropped, or every node's slot
                # is held by another submission) for --node-wait seconds
                while pending:
                    on_error(pending.popleft(), "no Ray worker node available for %d s" % args.node_wait)
            if not refs:
                if not pending:
                    break
                time.sleep(1)
            else:
                ready, _ = ray.wait(list(refs), num_returns=len(refs), timeout=0.5)
                for ref in ready:
                    kind, nid, t = refs.pop(ref)
                    a = actors.get(nid)
                    try:
                        val = ray.get(ref)
                    except Exception as e:
                        msg = "%s: %s" % (type(e).__name__, str(e).splitlines()[0][:300] if str(e) else "")
                        if kind == "info":
                            drop(nid, msg)
                            continue
                        if a:
                            a["inflight"] -= 1
                        if ("Actor" in type(e).__name__ or "Worker" in type(e).__name__
                                or "BrokenProcessPool" in str(e)):
                            drop(nid, msg)
                        retries[t] = retries.get(t, 0) + 1
                        if retries[t] <= args.retries:
                            pending.append(t)
                        else:
                            on_error(t, msg)
                        continue
                    if kind == "info":
                        if a:
                            a.update(ready=True, cores=val["cores"], ip=val["ip"], host=val["host"])
                            seen_ready[nid] = dict(val)
                            print("node %s (%s): %d cores" % (val["ip"], val["host"], val["cores"]),
                                  flush=True)
                    else:
                        if a:
                            a["inflight"] -= 1
                        on_result(val, actors.get(nid) or {"ip": "unknown", "host": "", "cores": 0})
            if time.time() - last_scan > SCAN_S:
                scan()
                last_scan = time.time()
            tick()
    finally:
        for a in actors.values():
            with contextlib.suppress(Exception):
                ray.kill(a["handle"])
    return {"nodes": len(seen_ready), "cores": sum(v["cores"] for v in seen_ready.values()),
            "head_ip": head_ip}


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------
def cmd_init(args):
    for name, val, lo, hi in (("tiles", args.tiles, 1, MAX_TILES), ("tile_px", args.tile_px, 8, MAX_TILE_PX),
                              ("max_iter", args.max_iter, 1, MAX_ITER)):
        if not lo <= val <= hi:
            raise UsageError("%s must be %d..%d, got %d" % (name, lo, hi, val))
    if args.palette not in core.PALETTES:
        raise UsageError("palette must be one of %s" % ", ".join(sorted(core.PALETTES)))
    region = core.DEFAULT_REGION
    if args.region:
        try:
            region = tuple(float(v) for v in args.region.split(","))
            assert len(region) == 4 and region[0] < region[2] and region[1] < region[3]
        except (ValueError, AssertionError):
            raise UsageError("region must be xmin,ymin,xmax,ymax with xmin<xmax, ymin<ymax")
    canvas = Canvas(args.canvas)
    dash = Dashboard(args.dashboard_url)
    sites = dash.site_metadata()  # read before /api/config wipes the registrations
    spec = {
        "canvas_id": canvas.id, "tiles": args.tiles, "tile_px": args.tile_px, "max_iter": args.max_iter,
        "palette": args.palette, "region": list(region),
        "preview_px": core.preview_size(args.tiles, args.tile_px),
        "dashboard_url": args.dashboard_url or None, "endpoint": args.endpoint or None,
        "commit": args.commit or None, "created": now_iso(), "sites": sites,
    }
    canvas.create(spec)
    if dash.url:
        dash.show_canvas(spec)
        dash.post("/api/phase", {"phase": "canvas_ready"})
    emit({"status": "canvas_ready", "canvas_id": canvas.id, "canvas_dir": canvas.dir,
          "tiles": args.tiles, "total_tiles": args.tiles ** 2, "tile_px": args.tile_px,
          "max_iter": args.max_iter, "palette": args.palette, "preview_px": spec["preview_px"],
          "dashboard": dash.url, "dashboard_errors": dash.failures,
          "dashboard_last_error": dash.last_error})
    return 0


def cmd_add(args):
    if args.count is not None and args.count < 0:
        raise UsageError("count must be >= 0")
    if args.count is None and not (args.all or args.rect or args.tile):
        raise UsageError("give --count K, --all, --rect x0,y0,x1,y1 or --tile x,y")
    canvas = Canvas(args.canvas)
    spec = canvas.spec()
    submission = args.submission or "manual-%s-%d-%d" % (socket.gethostname(), os.getpid(), int(time.time()))
    if not all(c.isalnum() or c in "-_." for c in submission):
        raise UsageError("submission id may contain letters, digits, - _ . only")
    dash = Dashboard(args.dashboard_url or spec.get("dashboard_url"))
    with canvas.locked():
        done = canvas.done()
        taken = set(done) | canvas.reserved(exclude=submission)
        tiles = select_tiles(spec, taken, args, zlib.crc32(submission.encode()))
        if tiles:
            canvas.reserve(submission, tiles)
    if not tiles:
        emit({"status": "added", "submission": submission, "canvas_id": canvas.id, "requested": 0,
              "rendered": 0, "failed": 0, "note": "no free tiles matched the request",
              "canvas": progress(spec, done)})
        return 0

    sites = dict(spec.get("sites") or {})
    sites_at = [0.0]
    per_site, failed, rendered, recorded = {}, [], [0], set()
    t0 = time.time()
    last_beat = [t0]
    next_report = [max(1, len(tiles) // 10)]
    if dash.url:
        dash.post("/api/phase", {"phase": "rendering"})

    def on_result(res, node):
        sid = site_of(node.get("ip"))
        if dash.url and time.time() - sites_at[0] > SITE_REFRESH_S:
            for k, v in dash.site_metadata().items():
                sites.setdefault(k, v)
            sites_at[0] = time.time()
        meta = sites.get(sid, {})
        entry = {"x": res["x"], "y": res["y"], "checksum": res["checksum"], "iter_sum": res["iter_sum"],
                 "ms": res["ms"], "node_ip": node.get("ip"), "host": node.get("host"), "site_id": sid,
                 "cluster_name": meta.get("cluster_name", ""), "submission": submission, "t": now_iso()}
        canvas.record(entry, res["png_b64"])
        recorded.add((res["x"], res["y"]))
        rendered[0] += 1
        s = per_site.setdefault(sid, {"tiles": 0, "cluster_name": meta.get("cluster_name", ""),
                                      "nodes": [], "ms": 0.0})
        s["tiles"] += 1
        s["ms"] += res["ms"]
        if node.get("ip") not in s["nodes"]:
            s["nodes"].append(node.get("ip"))
        if dash.url:
            dash.post("/api/tile", {"tile_x": res["x"], "tile_y": res["y"], "png_b64": res["png_b64"],
                                    "site_id": sid, "node_ip": node.get("ip"),
                                    "render_time_ms": res["ms"], "cluster_name": meta.get("cluster_name", ""),
                                    "scheduler_type": meta.get("scheduler_type", "")})
        if rendered[0] >= next_report[0]:
            el = time.time() - t0
            print("%d/%d tiles (%.1f tiles/s)" % (rendered[0], len(tiles), rendered[0] / el if el else 0),
                  flush=True)
            next_report[0] += max(1, len(tiles) // 10)

    def on_error(tile, msg):
        failed.append({"x": tile[0], "y": tile[1], "error": msg})

    def tick():
        if time.time() - last_beat[0] > HEARTBEAT_S:
            canvas.heartbeat(submission)
            last_beat[0] = time.time()

    print("submission %s: %d tiles on canvas %s (%dx%d, %d px, max_iter %d), backend %s"
          % (submission, len(tiles), canvas.id, spec["tiles"], spec["tiles"], spec["tile_px"],
             spec["max_iter"], args.backend), flush=True)
    backend_error = None
    try:
        if args.backend == "local":
            info = run_local(spec, tiles, args, on_result, on_error, tick)
        else:
            info = run_ray(spec, tiles, args, on_result, on_error, tick)
    except Exception as e:  # ray.init failed, the driver lost the cluster, ...
        backend_error = "%s: %s" % (type(e).__name__, e)
        info = {"nodes": 0, "cores": 0}
    finally:
        canvas.release(submission)
    accounted = {(f["x"], f["y"]) for f in failed} | set(recorded)
    for t in tiles:
        if t not in accounted:
            failed.append({"x": t[0], "y": t[1], "error": backend_error or "not rendered"})
    seconds = time.time() - t0
    done = canvas.done()
    prog = progress(spec, done, canvas.reserved())
    if prog["complete"]:
        prog["checksum"] = canvas_checksum(spec, done)
        if dash.url:
            dash.post("/api/phase", {"phase": "complete"})
    for s in per_site.values():
        s["avg_ms"] = round(s.pop("ms") / s["tiles"], 1) if s["tiles"] else 0.0
    status = "added" if not failed else ("partial" if rendered[0] else "failed")
    emit({"status": status, "submission": submission, "canvas_id": canvas.id, "requested": len(tiles),
          "rendered": rendered[0], "failed": len(failed), "failures": failed[:20],
          "seconds": round(seconds, 2), "tiles_per_s": round(rendered[0] / seconds, 2) if seconds else None,
          "nodes": info["nodes"], "cores": info["cores"], "per_site": per_site, "canvas": prog,
          "error": backend_error,
          "dashboard": dash.url, "dashboard_errors": dash.failures,
          "dashboard_last_error": dash.last_error})
    return 0 if not failed else 1


def cmd_status(args):
    canvas = Canvas(args.canvas)
    spec = canvas.spec()
    done = canvas.done()
    prog = progress(spec, done, canvas.reserved())
    if prog["complete"]:
        prog["checksum"] = canvas_checksum(spec, done)
    per_site, subs = {}, {}
    for e in done.values():
        s = per_site.setdefault(e.get("site_id", "?"), {"tiles": 0, "cluster_name": e.get("cluster_name", "")})
        s["tiles"] += 1
        subs[e.get("submission", "?")] = subs.get(e.get("submission", "?"), 0) + 1
    emit({"status": "status", "canvas_id": canvas.id, "canvas_dir": canvas.dir,
          "spec": {k: spec[k] for k in ("tiles", "tile_px", "max_iter", "palette", "region", "preview_px")},
          "canvas": prog, "per_site": per_site, "submissions": subs,
          "dashboard": spec.get("dashboard_url")})
    return 0


def cmd_redraw(args):
    canvas = Canvas(args.canvas)
    spec = canvas.spec()
    dash = Dashboard(args.dashboard_url or spec.get("dashboard_url"))
    if not dash.url:
        raise UsageError("no dashboard url (pass --dashboard-url)")
    done = canvas.done()
    dash.show_canvas(spec)
    dash.post("/api/phase", {"phase": "rendering"})
    for e in sorted(done.values(), key=lambda e: e.get("t", "")):
        dash.post("/api/tile", {"tile_x": e["x"], "tile_y": e["y"], "png_b64": canvas.preview_b64(e["x"], e["y"]),
                                "site_id": e.get("site_id", ""), "node_ip": e.get("node_ip", ""),
                                "render_time_ms": e.get("ms", 0), "cluster_name": e.get("cluster_name", "")})
    prog = progress(spec, done)
    dash.post("/api/phase", {"phase": "complete" if prog["complete"] else "canvas_ready"})
    emit({"status": "redrawn", "canvas_id": canvas.id, "tiles_posted": len(done), "canvas": prog,
          "dashboard": dash.url, "dashboard_errors": dash.failures, "dashboard_last_error": dash.last_error})
    return 0


def build_parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--canvas", required=True, help="canvas directory, e.g. ~/pw-fractal/<canvas-id>")
        sp.add_argument("--dashboard-url", default=None,
                        help="ray-cluster dashboard, e.g. http://localhost:<SESSION_PORT> (default: the canvas's)")

    sp = sub.add_parser("init", help="create a canvas and show it empty on the dashboard")
    common(sp)
    sp.add_argument("--tiles", type=int, required=True, help="tiles per side (canvas is N x N)")
    sp.add_argument("--tile-px", type=int, default=128, help="pixels per tile side (computed at full res)")
    sp.add_argument("--max-iter", type=int, default=256, help="escape-time iterations (work per pixel)")
    sp.add_argument("--palette", default="electric")
    sp.add_argument("--region", default=None, help="xmin,ymin,xmax,ymax (default: the whole set)")
    sp.add_argument("--endpoint", default=None, help="ray-cluster endpoint name (recorded only)")
    sp.add_argument("--commit", default=None, help="repo commit the canvas was created from (recorded only)")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("add", help="render more tiles into the canvas")
    common(sp)
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="every tile not yet done")
    g.add_argument("--rect", default=None, help="x0,y0,x1,y1 inclusive (tile coordinates)")
    g.add_argument("--tile", action="append", default=None, help="x,y (repeatable)")
    sp.add_argument("--count", type=int, default=None, help="at most K tiles (alone: the next K free tiles)")
    sp.add_argument("--order", default="random", choices=ORDERS)
    sp.add_argument("--submission", default=None, help="id recorded in the ledger (default: generated)")
    sp.add_argument("--backend", default="ray", choices=("ray", "local"))
    sp.add_argument("--address", default=None, help="Ray address (default: $RAY_ADDRESS or auto)")
    sp.add_argument("--processes", type=int, default=None, help="local backend: pool size")
    sp.add_argument("--cores-per-node", type=int, default=None,
                    help="ray backend: pool size per node (default: the node's usable cores)")
    sp.add_argument("--pool-start", default="spawn", choices=("spawn", "forkserver", "fork"),
                    help="ray backend: how each node's pool starts its processes")
    sp.add_argument("--node-wait", type=int, default=600,
                    help="ray backend: give up after this many seconds with no worker node")
    sp.add_argument("--retries", type=int, default=2, help="re-queue a failed tile this many times")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("status", help="canvas progress")
    common(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("redraw", help="re-post the canvas and all finished tiles to the dashboard")
    common(sp)
    sp.set_defaults(func=cmd_redraw)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UsageError as e:
        emit({"status": "error", "error": str(e)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
