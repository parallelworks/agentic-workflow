# fractal — a bag-of-tasks app for the Ray fleet

`app/heat/` is **tightly coupled**: every MPI rank exchanges halos with its neighbours
every iteration, so the job only runs inside one scheduler allocation, on one
cluster, and stalls if any rank is slow. This app is the opposite. It is an
**embarrassingly parallel bag of tasks**: a Mandelbrot image cut into an N × N
grid of tiles, each tile an independent task. Tiles can run anywhere, in any order,
on any mix of clusters, and a lost tile is simply re-queued. That workload is what
Ray is good at, and this app runs on the Ray cluster that `ray-launcher-mcp` stands up
across every cpu target in `sites/sites.yaml`.

| | heat (`app/heat`) | fractal (`app/fractal`) |
|---|---|---|
| coupling | halo exchange every iteration | none between tiles |
| runs on | one target's allocation (MPI) | the whole fleet at once (Ray) |
| unit of work | the whole job | one tile |
| scaling knob | ranks per target | nodes in the Ray cluster |
| correctness | relative L2 vs a reference field | canvas checksum (deterministic) |

## Files

- `fractal_core.py`: renders one tile. It is pure compute (NumPy, with a pure-Python
  fallback that gives the identical result), with no Ray and no I/O.
- `render.py`: the driver and CLI. It manages a canvas, picks tiles, runs them on Ray
  (or locally) and posts them to the Ray dashboard.

## The canvas

A canvas is one fixed picture of `tiles × tiles` tiles. It starts **empty** on the
dashboard and fills in as tiles are added, a few or many at a time, by agents or
people, possibly concurrently. Its state lives on the Ray head host in
`~/pw-fractal/<canvas-id>/`:

| file | what |
|---|---|
| `canvas.json` | spec: `tiles`, `tile_px`, `max_iter`, `palette`, `region`, `preview_px`, dashboard URL, endpoint, commit |
| `tiles.jsonl` | ledger: one line per finished tile (x, y, checksum, ms, node, site, cluster, submission) |
| `previews/` | the small PNG each tile showed on the dashboard (used by `redraw`) |
| `reserved/` | tiles claimed by submissions still running; a claim expires 10 min after its submission stops heartbeating |
| `lock` | `flock` that serializes tile selection, so concurrent submissions never render the same tile |

## Knobs and cost

- `tiles` (1 to 256 per side) sets how many tasks there are: `tiles²` in all.
- `tile_px` (8 to 4096) and `max_iter` set how much work each task does. Work per
  tile is roughly `tile_px² × (iterations per pixel, at most max_iter)`.
- The tiles near the set's boundary cost the most; tiles outside it are cheap. So
  `--order random` spreads the load, and `center`/`spiral` render the busy middle
  first.

Each tile is computed at full `tile_px` resolution; that is the real work. The
dashboard only gets a preview of `min(tile_px, max(4, 2048 // tiles))` px. That keeps
its canvas near 2048 px and a 100 × 100 canvas in the tens of MB, whatever `tile_px`
is.

Scaling experiments to try:
- **Strong scaling:** add the same count of tiles on clusters with more nodes.
- **Weak scaling:** grow the count with the node count.
- **Work per task:** raise `tile_px` or `max_iter` and watch the tiles/s and the
  per-site average ms in the result line.

## How work reaches the cluster

1. `render.py add` is the Ray **driver**. Run it on the head host, normally as a Ray
   Job. Remote sites' workers join over SSH tunnels from the head, so only that host
   reaches every worker.
2. The ray-cluster workers start with `ray start --num-cpus=1`, so **each node has one
   Ray slot**. The driver places **one actor per worker node** (NodeAffinity,
   `num_cpus=1`). The actor runs a **process pool over the node's usable cores**
   (`os.sched_getaffinity`, or `--cores-per-node`), so a node renders as many tiles
   at once as it has cores.
3. The actor receives `fractal_core.py` as source text from the driver and imports it
   from a node-local temp dir named by its hash. Worker nodes need only Ray and NumPy,
   not this repo.
4. The driver keeps 2 × cores tiles in flight per node. It collects results as they
   finish and, for each: appends it to the ledger, saves the preview and POSTs it to
   the dashboard (`/api/tile`). Every 10 s it adds actors for newly joined nodes.
5. A submission holds every node's slot while it runs. A second concurrent submission
   waits for slots, up to `--node-wait`, default 600 s.

The dashboard is the ray-cluster dashboard. `init` POSTs `/api/config` once, which
resets it to the empty grid. `add` only POSTs tiles. A browser reload redraws from
`/api/state`, and `render.py redraw` re-posts everything after a dashboard restart.
Tiles are coloured by **site**: `site-1` is the head's own cluster, and `site-X` is
the remote worker row reached on `127.0.X.*`.

## Commands

Each prints progress lines and ends with ONE JSON line. Exit code 0 means ok, 1 means
some tiles failed, and 2 means a usage or canvas error.

```
render.py init   --canvas DIR --tiles N [--tile-px P] [--max-iter M] [--palette electric|fire|ocean|cosmic]
                 [--region xmin,ymin,xmax,ymax] [--dashboard-url URL]
render.py add    --canvas DIR (--count K | --all | --rect x0,y0,x1,y1 | --tile x,y ...)
                 [--order random|row|center|spiral] [--backend ray|local] [--submission ID]
render.py status --canvas DIR
render.py redraw --canvas DIR [--dashboard-url URL]
```

## Running it

**With the agents** (see AGENTS.md):
1. `ray-launcher-mcp` launches the cluster.
2. `ray-runner-mcp` makes canvases (`new_canvas`) and renders tiles (`render_tiles`,
   `render_status`).

**By hand through the workflow:** this is the same path the agent takes, and it is
platform-logged.
1. Copy a staged `runs/ray-runner-mcp-fleet-*/fractal.inputs.json`.
2. Set `action: add`, `canvas_id`, a new `run_id`, and `count` (or `all: "true"`, or
   `rect`).
3. Run:
   ```
   pw workflows run -i <inputs.json> --name <run_id> workflows/fractal.yaml
   ```

**Ray Jobs from your machine:** open the SSH tunnel to port 8265 shown on the
dashboard's Connect tab, then:
```
ray job submit --address http://localhost:8265 --working-dir app/fractal -- \
  python render.py add --canvas '~/pw-fractal/<canvas-id>' --count 50
```
`--canvas` is a path on the head host. Quote `~` so your shell does not expand it
locally. If the head's `python` is not the cluster's venv (the job then fails with
`No module named 'ray'`), give the venv's interpreter instead. That is the path in
the job dir's `RAY_VENV_DIR`, plus `/bin/python`; `fractal.yaml` always does this.

**Directly on the workspace (head host):**
```
JOB=<the cluster's job dir, the one whose ENDPOINT_NAME is ray-cluster-<slug>>
source "$(cat $JOB/RAY_VENV_DIR)/bin/activate"
export RAY_ADDRESS=$(cat $JOB/RAY_HEAD_IP):6379
python app/fractal/render.py init --canvas ~/pw-fractal/my-canvas --tiles 40 --tile-px 64 \
  --dashboard-url http://localhost:$(cat $JOB/SESSION_PORT)
python app/fractal/render.py add --canvas ~/pw-fractal/my-canvas --count 200
```

**Locally, no Ray** (for testing):
`render.py init --canvas /some/dir --tiles 4 --tile-px 32` and then
`render.py add --canvas /some/dir --all --backend local`.

The cluster is a general bag-of-tasks pool, so any other Ray script works the same
way.

## Correctness

Escape counts are integers computed with the same float64 operations in the same
order on every path, so a tile's checksum (sha256 of its counts) is reproducible
anywhere. A complete canvas has one checksum: sha256 over its tiles' checksums in
row order. It is independent of how many submissions, nodes or sites rendered it.
`render.py add --backend local --all` on a canvas with the same spec gives the
reference value. Preview colours are not part of the checksum.
