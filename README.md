# AI Direct UV Layout Agent

An AI agent that performs **UV layout on 3D meshes directly** — not just calling
Blender's `unwrap`/`pack_islands` operators, but generating UV islands, writing
UV coordinates, resolving overlap, minimizing stretch, and optimizing packing.

This repo implements the engine core from [`docs/PLAN.ko.md`](docs/PLAN.ko.md).
The guiding split is:

```
LLM     = intent understanding + strategy + repair planning
Solver  = coordinate computation + constraint optimization + validation
Blender = mesh source + UV write + preview render
```

The LLM never emits raw UV coordinates. It emits **structured actions**; a
deterministic geometry solver computes the actual layout. That keeps results
stable and fully testable.

## What's implemented

| Plan phase | Module | Status |
| --- | --- | --- |
| 1. Direct UV write | `uv_agent/blender/apply.py` (`AI_UV` layer write) | ✅ |
| 2. Mesh graph extractor | `uv_agent/geometry/mesh_graph.py`, `uv_agent/blender/extract.py` | ✅ |
| 3. Island planner | `uv_agent/planner/island_planner.py`, `operations.py` | ✅ |
| 4. UV coordinate solver | `uv_agent/geometry/projection.py`, `relaxation.py` | ✅ |
| 5. Packing optimizer | `uv_agent/geometry/packing.py` | ✅ |
| 6. Quality evaluator | `uv_agent/geometry/evaluation.py`, `preview.py` (SVG) | ✅ |
| 7. LLM agent loop | `uv_agent/agent/` (schema, providers, pipeline) | ✅ |
| 8-10. Web app / memory / productization | — | planned (see PLAN §8, §12) |

Everything in `geometry/`, `planner/`, `agent/`, `io/` is **pure Python (numpy
only)** and runs without Blender, so the whole
`plan → generate → pack → evaluate → repair` loop is unit-tested (`pytest`, 35
tests). The `blender/` package is a thin adapter that lazily imports
`bpy`/`bmesh` and only runs inside Blender.

## Quick start (no Blender required)

```bash
pip install -e .            # numpy only
pip install -e '.[dev]'     # + pytest
pytest                      # 35 tests, ~0.1s

# Run the full agent pipeline on a synthetic mesh and write artifacts:
python -m uv_agent.demo --shape cylinder --out out/demo
python -m uv_agent.demo --shape cube     --out out/cube
```

The demo prints the repair trace and writes `solution.json`, `result.json` and
`preview.svg` (open the SVG to see the packed UV islands). Example trace for a
tube that starts with the wrong (planar) projection:

```
shape=cylinder provider=mock
iterations=2 islands=1 status=accepted
  it0: status=needs_repair stretch=2.898 overlap=0.500 -> ['set_island_projection']
  it1: status=accepted     stretch=0.000 overlap=0.000 -> []
```

The agent detected the fold (overlap 0.5) + stretch, switched the island to a
cylindrical projection, and the re-evaluation passed.

## Test it inside Blender (with a real object)

`scripts/blender_unwrap_active.py` runs the agent on a Blender object, writes the
result into an **`AI_UV`** UV map (and makes it active), and optionally saves a
preview / `.blend`. Tested against Blender 5.0 (bundled Python 3.11 + numpy).

**A) See it live in the Blender GUI** (recommended — no copy/paste, no path setup):

```bash
BLENDER=/Applications/Blender.app/Contents/MacOS/Blender   # macOS path

# Add a test primitive and open the GUI with UVs already applied:
"$BLENDER" --python scripts/blender_unwrap_active.py -- --add cylinder

# ...import a model file (obj/fbx/gltf/glb/stl/ply):
"$BLENDER" --python scripts/blender_unwrap_active.py -- --import sample/uv_no.obj

# ...or run on an object in your own .blend:
"$BLENDER" my_model.blend --python scripts/blender_unwrap_active.py -- --object RobotArm
```

Then switch to the **UV Editing** workspace; the `AI_UV` map shows the layout.
`--add` accepts: `cube`, `suzanne`, `uvsphere`, `cylinder`, `torus`.
`--import` accepts: `.obj`, `.fbx`, `.gltf`, `.glb`, `.stl`, `.ply`.

**B) Headless (no UI), save artifacts to inspect:**

```bash
"$BLENDER" --background --python scripts/blender_unwrap_active.py -- \
    --add cylinder --svg out/cyl.svg --save out/cyl_AI_UV.blend
# open out/cyl.svg to see the packed islands; open the .blend in 'UV Editing'.
```

**C) From Blender's Scripting tab:** select your mesh, set the repo path once, run:

```python
import os; os.environ["UV_AGENT_REPO"] = "/path/to/brisbane"   # this repo
exec(open("/path/to/brisbane/scripts/blender_unwrap_active.py").read())
```

Options after `--`: `--object NAME` | `--add SHAPE` | `--import PATH`,
`--provider {mock,openai_oauth_local,openai_api_key}`, `--intent "..."`,
`--angle 30`, `--padding 8`, `--texture 1024`,
`--split-smoothing-by-uv-islands`, `--svg PATH`, `--save PATH`.

Verified results (Blender 5.0, `mock` provider):

| Object | Result |
| --- | --- |
| Cube | `accepted` — 6 islands, overlap 0, stretch 0 |
| Cylinder (primitive) | `accepted` — body unwrapped cylindrically + 2 planar caps, overlap 0 |
| Suzanne / Torus | valid layout written, flagged `needs_repair` (curved/organic — out of MVP scope, §16) |

For the curved/organic cases the agent still writes a usable layout and keeps the
*best* iteration; reaching `accepted` there needs the post-MVP solvers (xatlas/
libigl, plan §11) or a real LLM provider instead of the mock heuristic.

### Split smoothing by UV islands (optional)

A post-processing option that breaks normal smoothing at the UV island borders.
Blender has no 3ds-Max-style smoothing-group ids — a normal split is expressed
as a **sharp edge** on top of **smooth faces**. So this option marks every UV
island boundary edge (the same edges as the UV seams) as *sharp* while keeping
all faces *smooth*, so shading stays smooth inside an island and breaks exactly
at the seams.

It is **off by default** (existing behavior is unchanged). It does not touch the
UV unwrap, packing or preview, and it only *adds* sharp flags (existing sharp
edges are preserved).

CLI:

```bash
"$BLENDER" --python scripts/blender_unwrap_active.py -- \
    --add cylinder --split-smoothing-by-uv-islands
# log: [AI-UV] split smoothing by UV islands: 64 sharp edges
```

Worker job JSON:

```json
{
  "provider": "mock",
  "user_intent": "unwrap and split smoothing by UV islands",
  "split_smoothing_by_uv_islands": true
}
```

Programmatic (inside Blender):

```python
from uv_agent.blender.apply import apply_uv_coordinates, apply_smoothing_split_by_edges
apply_uv_coordinates(obj, result.solution, seam_edge_ids=result.plan.seam_edge_ids)
apply_smoothing_split_by_edges(obj, result.plan.seam_edge_ids)  # smooth_faces=True
```

Verified in Blender 5.0: on the cylinder the sharp edge set equals the seam edge
set (64 == 64) and all faces remain smooth, so smoothing visibly splits at the
island borders. Future extensions (plan): `split_smoothing_by_material`,
`split_smoothing_by_angle`, `preserve_existing_sharp_edges`,
`clear_previous_ai_sharp_edges`.

## Architecture

```
uv_agent/
  geometry/        deterministic engine (numpy)
    mesh_graph.py    Vertex/Edge/Face/Loop + builder + JSON I/O (plan §7.1)
    solution.py      UVMap (per-loop) + UVSolution (plan §7.3)
    projection.py    planar + cylindrical + strip/grid straightening (Phase 4)
    relaxation.py    boundary-preserving Laplacian relax
    packing.py       MaxRects + shelf packing into [0,1], auto-select (Phase 5)
    evaluation.py    overlap/stretch/angle/texel/packing/seam metrics (Phase 6)
    preview.py       SVG UV-layout preview
  planner/
    island_planner.py  hard-edge / material / seam island split (Phase 3)
    operations.py      split/merge/protect actions (plan §7.6)
  agent/
    schema.py        JSON schema for {intent, plan, success_criteria} (§10.2)
    llm.py           LLMProvider + Mock + OpenAI (oauth-proxy) providers (§8.4)
    pipeline.py      plan→generate→pack→evaluate→repair orchestrator (Phase 7)
  blender/           bpy/bmesh adapter, lazy-imported (plan §9)
    extract.py         bmesh -> MeshGraph
    apply.py           UVSolution -> AI_UV layer + checker material
  io/fixtures.py     synthetic cube/plane/cylinder for tests + demos
  io/obj_loader.py   minimal Wavefront OBJ -> MeshGraph (offline, no Blender)
worker/run_uv_job.py headless Blender worker entrypoint (plan §9.2)
tests/               pytest suite
```

### Packing strategies (packing efficiency)

`pack_islands(..., strategy=...)` supports:

- `"auto"` (default) — runs both packers below and keeps whichever yields the
  higher `packing_efficiency`, so the result is **never worse than shelf** and
  improves on irregular island sets.
- `"maxrects"` — MaxRects (Best-Short-Side-Fit) with 0/90 rotation; fills gaps
  between differently-sized islands.
- `"shelf"` — original row/shelf packer.

All three keep a single global scale (texel density preserved) and are
overlap-free.

### Strip / grid straightening (curved bands → straight)

Planar projection leaves curved bevel/cylinder bands *bent* in UV space. The
default projection path auto-detects quad **strips** (1-wide chains,
`project_island_strip`) and quad **grids** (M×N patches, `project_island_grid`,
Follow-Active-Quads style) and *unrolls* them — accumulating real arc length
along U and width along V — so they come out as straight rectangles instead of
arcs. Non-quad / irregular patches fall back to planar.

Combined with the MaxRects packer, on `aaa_no_uv.obj` (1397 faces, 144 islands):

| | shelf + planar | + MaxRects | + strip/grid straightening |
| --- | --- | --- | --- |
| `packing_efficiency` | 0.297 | 0.354 | **0.693** |
| `stretch_score` | 0.179 | 0.179 | **0.045** |
| `overlap_ratio` | 0 | 0 | 0 |

90 of 144 islands are straightened (64 strips + 26 grids); the rest are
irregular patches that stay planar. This implements the Phase-3/4 items of
`.context/uv_sample_alignment_plan.ko.md`; PCA axis-alignment and category-based
lane layout remain future work.

## LLM providers (plan §3, §8.4)

Default provider is the local [`openai-oauth`](https://github.com/EvanZhouDev/openai-oauth)
proxy, used for **personal local use only** (not for hosting/sharing — see plan
§3 "제한 및 주의").

```bash
# 1. log in to Codex/ChatGPT, then start the local proxy:
npx openai-oauth          # serves http://127.0.0.1:10531/v1
```

```python
from uv_agent.agent.llm import get_provider
from uv_agent.agent.pipeline import UVAgentPipeline

provider = get_provider("openai_oauth_local")   # default local proxy
# provider = get_provider("openai_api_key")      # fallback: official API + OPENAI_API_KEY
# provider = get_provider("mock")                # offline, deterministic

result = UVAgentPipeline(provider).run(mesh_graph, "unwrap for hard-surface texturing")
```

Override the endpoint / model via env or args:

```bash
export UV_AGENT_BASE_URL=http://127.0.0.1:10531/v1
```

`OpenAIProvider` lazy-imports `openai` (`pip install 'uv-agent[llm]'`).

## Running inside Blender

```bash
blender --background project.blend \
    --python worker/run_uv_job.py -- --job job.json
```

`job.json`:

```json
{
  "job_id": "job_123",
  "object_name": "robot_arm_001",
  "user_intent": "hard-surface texturing unwrap",
  "provider": "mock",
  "angle_threshold": 30,
  "padding_px": 8,
  "texture_size_px": 1024,
  "out_dir": "out/job_123"
}
```

The worker extracts the mesh graph, runs the agent pipeline, writes UVs back
into the `AI_UV` layer (validating loop indices), applies a checker material,
and saves `solution.json`, `evaluation.json`, `preview.svg`, and a `preview.png`
render.

## Quality metrics (plan §7.5)

`evaluate_uv_solution` returns: `overlap_ratio` (folded/flipped UV area),
`stretch_score` (area-weighted log area distortion), `angle_distortion`,
`texel_density_variance`, `packing_efficiency`, `seam_visibility_score` (proxy),
`island_count`, `small_island_ratio`, and an accept/`needs_repair` `status`.

## Not yet built (roadmap)

Phases 8–10 of the plan — Next.js/NestJS web app, Postgres/Prisma data model
(§13), REST API (§14), BullMQ job queue, semantic memory retrieval (§8.3), and
productization — are **designed but not implemented here**. This repo is the
deterministic engine + agent core they would sit on top of. The pipeline already
returns a fully JSON-serializable `RunResult`, ready to be persisted as the
`AgentRun` / `UVResult` records in that schema.

## Security note (plan §15)

`openai-oauth` is an **unofficial** project. The local OAuth auth file is a
sensitive credential. This project is for **personal local use** only; do not
host it, share tokens, or expose the proxy externally. `.gitignore` excludes
`auth.json`, `oauth_cache/`, `.env`, and `*.blend`.
