# Electron UV Review App MVP 1 Production Plan

> 기준 PRD: `docs/ELECTRON_UV_REVIEW_APP_PRD.ko.md`  
> 선행 계약: `docs/ELECTRON_UV_REVIEW_APP_MVP0_PRODUCTION_PLAN.ko.md`  
> 범위: MVP 1 UV Review App을 production-ready 단계로 끌어올리기 위한 구현 계획  
> 대상: 다른 Conductor 세션, Electron app 작업자, Python/Blender worker 작업자, UV metric/preview 작업자, QA 작업자  
> 핵심 목표: MVP 0에서 승인된 low-poly working model 또는 사용자가 직접 import한 low-poly 모델의 기존 UV를 읽고, UV layout/checker/metric report를 안정적으로 검토할 수 있게 한다.

---

## 1. MVP 1 정의

MVP 1은 UV 생성기가 아니다. 기존 UV를 읽고 검토하는 앱 단계다.

MVP 1의 제품 완료 상태:

```text
MVP 0 working low-poly
  또는 direct low-poly import
  -> model/UV layer inspection
  -> active UV layer 선택
  -> UV layout image 생성
  -> checker preview 생성
  -> stretch/overlap/density/packing report 생성
  -> UI에서 review
  -> MVP 2 seam editor 또는 MVP 3 generate/optimize로 넘길 준비 완료
```

MVP 1에서 반드시 보장할 것:

- `project.json`의 `working_model`을 읽을 수 있다.
- FBX/OBJ/GLB/GLTF direct low-poly import도 가능하다.
- mesh summary와 UV layer 목록을 UI에 표시한다.
- 기존 UV layer가 있으면 UV layout PNG를 생성한다.
- active UV layer 기준 checker front/side preview를 생성한다.
- UV metric report를 JSON으로 저장하고 UI에 표시한다.
- UV가 없는 모델은 명확한 empty state를 보여주고, 이후 MVP 2/3로 넘길 수 있는 상태를 만든다.
- mandatory 90 rule은 MVP 1 review/gate에 관여하지 않는다.

MVP 1에서 하지 않을 것:

- seam/chapter edge editing
- UV unwrap, relax, pack, optimize
- user seam spec 저장
- AI/Nemotron review
- final production export
- UV를 자동 수정하는 repair

중요한 제품 원칙:

- MVP 1은 read-only review 단계다.
- 기존 UV coordinates, seam flags, material assignment를 사용자 승인 없이 변경하지 않는다.
- preview material 적용은 worker 내부 임시 scene에서만 수행한다. 원본 working model을 덮어쓰지 않는다.

---

## 2. MVP 0과의 연결

MVP 0 완료 후 `project.json`은 최소한 다음 값을 가진다.

```json
{
  "working_model": "work/working_lowpoly.blend",
  "working_model_fbx": "work/working_lowpoly.fbx",
  "approved_lowpoly_run_id": "run_uuid",
  "selected_object": "SM_Test_Pottery_a_02"
}
```

MVP 1은 기본적으로 `working_model`을 입력으로 사용한다. `working_model`이 없으면 다음 fallback을 허용한다.

1. `working_model_fbx`
2. 사용자가 직접 import한 low-poly file
3. MVP 0 inspect 결과만 있는 source file

MVP 1 run은 기존 project folder에 append-only로 저장한다.

```text
<project>/
  runs/
    <review_run_id>/
      job.json
      status.json
      stdout.log
      stderr.log
      uv_review_summary.json
      uv_layers.json
      uv_metrics.json
      uv_bounds.json
      uv_layout.png
      checker_front.png
      checker_side.png
      checker_3q.png optional
      stretch_heatmap.png optional
      overlap_mask.png optional
```

`project.json`에는 최신 review run만 pointer로 추가한다.

```json
{
  "latest_uv_review_run_id": "review_run_uuid",
  "uv_review_runs": ["review_run_uuid"]
}
```

---

## 3. 현재 코드 기반에서의 출발점

이미 존재하는 관련 자산:

```text
uv_agent/blender/extract.py
uv_agent/geometry/evaluation.py
uv_agent/geometry/preview.py
worker/run_uv_job.py
worker/run_quad_retopo_job.py
```

재사용 가능한 기능:

- Blender object -> `MeshGraph` 추출
- UV overlap/stretch/texel density/packing 계열 평가 함수
- UV layout SVG 생성 함수
- checker material 적용 로직
- `worker/run_quad_retopo_job.py` 내부 checker render / UV layout export helper

MVP 1에서 새로 고정해야 하는 것:

- 기존 Blender UV layer를 `UVMap`으로 읽는 adapter
- 기존 UV layer를 기준으로 island를 추출하는 adapter
- `review_existing_uv` worker command
- Electron에서 소비할 normalized `uv_review_summary.json`
- UI report contract

주의:

- `worker/run_uv_job.py`는 새 UV를 생성하는 worker다. MVP 1 production path의 기본 worker로 쓰지 않는다.
- `worker/run_quad_retopo_job.py`의 helper는 참고/이식 대상이지, MVP 1 command entrypoint가 아니다.

---

## 4. Production Architecture

MVP 1은 MVP 0과 같은 3-layer 구조를 유지한다.

```text
Electron Renderer
  - UV review workspace
  - object/UV layer selector
  - checker preview view
  - UV layout view
  - metrics/report panel
  - empty/no-UV state

Electron Main
  - project.json 읽기/갱신
  - working model path resolve
  - Blender worker spawn
  - run status/log/artifact 관리
  - IPC API 제공

Python/Blender Worker
  - inspect_uv_layers
  - review_existing_uv
  - render_checker_preview
  - export_uv_layout
  - compute_uv_metrics
```

Production 원칙:

- Renderer는 artifact path와 normalized JSON만 읽는다.
- Renderer는 Blender stdout/stderr를 parse하지 않는다.
- Worker는 active UV layer를 명시적으로 받아야 한다.
- UV layer가 없으면 실패가 아니라 `status: no_uv`로 반환한다.
- 모든 image artifact는 project-relative path로 summary에 기록한다.

---

## 5. Worker API Contract

### 5.1 `inspect_uv_layers`

목적: working model의 object와 UV layer 목록을 읽는다. MVP 0의 `inspect_model`보다 UV review에 필요한 정보를 더 포함한다.

Input:

```json
{
  "command": "inspect_uv_layers",
  "project_id": "project_uuid",
  "model": "/absolute/path/to/project/work/working_lowpoly.blend",
  "object_name": "SM_Test_Pottery_a_02"
}
```

Output:

```json
{
  "schema_version": 1,
  "status": "accepted",
  "model": "work/working_lowpoly.blend",
  "objects": [
    {
      "name": "SM_Test_Pottery_a_02",
      "vertices": 6562,
      "edges": 18701,
      "faces": 12152,
      "materials": ["default"],
      "uv_layers": [
        {
          "name": "UVChannel_1",
          "active": true,
          "loop_count": 36396,
          "empty": false
        }
      ],
      "active_uv_layer": "UVChannel_1",
      "has_uv": true
    }
  ],
  "recommended_next_step": "review_existing_uv"
}
```

No-UV Output:

```json
{
  "schema_version": 1,
  "status": "no_uv",
  "objects": [
    {
      "name": "Lowpoly",
      "vertices": 1200,
      "edges": 3400,
      "faces": 2200,
      "uv_layers": [],
      "active_uv_layer": null,
      "has_uv": false
    }
  ],
  "recommended_next_step": "open_seam_editor_or_generate_uv"
}
```

### 5.2 `review_existing_uv`

목적: 기존 UV layer를 기준으로 layout/checker/metric report를 생성한다.

Input:

```json
{
  "command": "review_existing_uv",
  "project_id": "project_uuid",
  "run_id": "review_run_uuid",
  "model": "/absolute/path/to/project/work/working_lowpoly.blend",
  "object_name": "SM_Test_Pottery_a_02",
  "uv_layer": "UVChannel_1",
  "options": {
    "texture_size_px": 1024,
    "checker_scale": 40.0,
    "render_size_px": 900,
    "raster_overlap_resolution": 1024,
    "make_heatmaps": false
  },
  "out_dir": "/absolute/path/to/project/runs/review_run_uuid"
}
```

Output:

```json
{
  "schema_version": 1,
  "run_id": "review_run_uuid",
  "command": "review_existing_uv",
  "status": "accepted",
  "model": "work/working_lowpoly.blend",
  "object_name": "SM_Test_Pottery_a_02",
  "uv_layer": "UVChannel_1",
  "mesh": {
    "vertices": 6562,
    "edges": 18701,
    "faces": 12152,
    "loops": 36396
  },
  "uv": {
    "island_count": 43,
    "uv_bounds": {
      "min": [0.001, 0.002],
      "max": [0.998, 0.997],
      "in_0_1": true
    },
    "has_negative_uv": false,
    "has_out_of_tile_uv": false
  },
  "metrics": {
    "stretch_score": 0.06866,
    "worst_island_distortion": 0.202999,
    "overlap_ratio": 0.0,
    "raster_overlap_ratio": 0.0,
    "self_overlap_ratio": 0.0,
    "cross_overlap_ratio": 0.0,
    "texel_density_variance": 0.000002,
    "packing_efficiency": 0.591278
  },
  "artifacts": {
    "summary": "uv_review_summary.json",
    "metrics": "uv_metrics.json",
    "uv_layout": "uv_layout.png",
    "checker_front": "checker_front.png",
    "checker_side": "checker_side.png",
    "checker_3q": null,
    "overlap_mask": null,
    "stretch_heatmap": null
  },
  "warnings": []
}
```

No-UV Output:

```json
{
  "schema_version": 1,
  "run_id": "review_run_uuid",
  "command": "review_existing_uv",
  "status": "no_uv",
  "object_name": "SM_Test_Pottery_a_02",
  "uv_layer": null,
  "metrics": null,
  "artifacts": {},
  "warnings": ["Object has no UV layer to review."]
}
```

### 5.3 `set_active_uv_layer`

목적: UI에서 선택한 UV layer를 review input으로 저장한다. 원본 model을 수정하지 않고 project state만 갱신한다.

Input:

```json
{
  "command": "set_active_uv_layer",
  "project_id": "project_uuid",
  "object_name": "SM_Test_Pottery_a_02",
  "uv_layer": "UVChannel_1"
}
```

Output:

```json
{
  "status": "accepted",
  "selected_uv_layer": "UVChannel_1"
}
```

`project.json` 추가 필드:

```json
{
  "selected_uv_layer": "UVChannel_1"
}
```

---

## 6. UV Evaluation Requirements

MVP 1 report는 review용이다. UV를 수정하거나 gate fail로 flow를 막지 않는다.

Required metrics:

- `stretch_score`
- `worst_island_distortion`
- `overlap_ratio`
- `raster_overlap_ratio`
- `self_overlap_ratio`
- `cross_overlap_ratio`
- `texel_density_variance`
- `packing_efficiency`
- `island_count`
- `uv_bounds`

Metric semantics:

- `overlap_ratio`: signed/flipped area 기반 diagnostic
- `raster_overlap_ratio`: 실제 UV interior overlap diagnostic
- `texel_density_variance`: island 간 density 불균일성
- `packing_efficiency`: UV used area / UV global bbox area
- `worst_island_distortion`: per-island checker/stretch summary의 worst value

Mandatory 90 rule:

- MVP 1에서는 계산하지 않는다.
- MVP 1에서는 gate로 쓰지 않는다.
- PRD상의 `mandatory 90 rule은 기본 report/gate에 끼지 않는다`를 그대로 따른다.

Threshold policy:

- MVP 1은 pass/fail보다 `review_status`를 표시한다.
- `review_status` 값:
  - `clean`
  - `has_overlap`
  - `high_stretch`
  - `density_variance`
  - `out_of_bounds`
  - `no_uv`
  - `unknown`
- 여러 문제가 있으면 `issues` array에 모두 넣는다.

Example:

```json
{
  "review_status": "has_overlap",
  "issues": [
    {
      "code": "raster_overlap",
      "severity": "error",
      "message": "UV islands overlap in raster check.",
      "metric": "raster_overlap_ratio",
      "value": 0.012
    }
  ]
}
```

---

## 7. Image Artifact Requirements

Required artifacts:

```text
uv_layout.png
checker_front.png
checker_side.png
```

Optional artifacts:

```text
checker_3q.png
overlap_mask.png
stretch_heatmap.png
uv_layout.svg
```

Rules:

- `uv_layout.png`는 active UV layer 기준이어야 한다.
- checker render는 active UV layer를 사용해야 한다.
- checker material은 원본 model에 저장하지 않는다.
- front/side camera framing은 같은 object bounds를 기준으로 안정적으로 잡는다.
- image size는 기본 900px 또는 1024px로 고정한다.
- artifact path는 summary JSON에 project-relative로 저장한다.

초기 구현 선택:

- UV layout PNG는 Blender `bpy.ops.uv.export_layout`을 우선 사용한다.
- checker render는 `worker/run_quad_retopo_job.py`의 `_apply_checker_uv`, `_render_checker` 로직을 MVP 1 worker로 복사/정리한다.
- heatmap은 MVP 1.1로 미뤄도 된다. 필수 blocker가 아니다.

---

## 8. Electron MVP 1 UX

MVP 1 첫 화면은 UV review workspace다. landing/marketing page를 만들지 않는다.

Required layout:

```text
Top Bar
  Open Project | Import Low-poly | Inspect UV | Review UV | Next: Seam Editor

Left Panel
  Project
  Objects
  UV Layers
  Review Runs

Center
  Tabs: Checker | UV Layout
  Checker view: front / side toggle
  UV Layout view: image with pan/zoom

Right Panel
  Mesh Summary
  UV Summary
  Issues
  Metrics

Bottom Panel
  Run status
  Logs
  Artifact links
```

Required interactions:

- working model load
- direct low-poly import
- object select
- UV layer select
- review run start
- checker front/side toggle
- UV layout zoom/pan
- report JSON display
- no-UV empty state
- artifact open/reveal

No-UV empty state:

```text
No UV layer found.
Use MVP 2 to mark seams or MVP 3 to generate UVs.
```

UI wording rule:

- Metric labels must be artist-readable.
- Raw JSON should be available in a report tab but not be the only UI.
- Do not claim UV is "good" or "production-ready" solely from metrics. Use "No blocking review issue detected" at most.

---

## 9. Project State Contract

`project.json` MVP 1 extension:

```json
{
  "selected_object": "SM_Test_Pottery_a_02",
  "selected_uv_layer": "UVChannel_1",
  "latest_uv_review_run_id": "review_run_uuid",
  "uv_review_runs": ["review_run_uuid"]
}
```

`runs/<review_run_id>/status.json`:

```json
{
  "schema_version": 1,
  "run_id": "review_run_uuid",
  "command": "review_existing_uv",
  "status": "queued | running | accepted | no_uv | failed | cancelled",
  "started_at": "2026-06-20T00:00:00.000Z",
  "finished_at": null,
  "input": {
    "model": "../../work/working_lowpoly.blend",
    "object_name": "SM_Test_Pottery_a_02",
    "uv_layer": "UVChannel_1"
  },
  "artifacts": {},
  "error": null
}
```

`runs/<review_run_id>/uv_review_summary.json` is the primary UI input. Renderer must not assemble metrics by reading multiple raw worker files unless needed for debug tabs.

---

## 10. 병렬 작업 분해

다른 세션에 나눠 맡길 때 아래 단위로 분리한다. 한 세션이 여러 영역의 파일을 동시에 소유하지 않게 한다.

### Session A: Existing UV Extraction + Metrics

Owner files:

```text
uv_agent/blender/uv_extract.py
uv_agent/geometry/uv_review.py
tests/test_uv_review_metrics.py
```

Tasks:

- Blender UV layer -> per-loop `UVMap` adapter 구현
- active/specified UV layer 선택 로직 구현
- UV island 추출 또는 face chart 추정 구현
- `uv_bounds`, `island_count`, `packing_efficiency` 계산 helper 구현
- `worst_island_distortion` 계산 helper 정리
- pure Python 가능한 metric helper는 unit test 작성

Acceptance:

- UV가 있는 mesh에서 loop UV count가 mesh loop count와 일치한다.
- UV bounds와 island count가 deterministic하게 나온다.
- no-UV object는 exception 대신 typed result를 반환한다.

### Session B: Review Worker

Owner files:

```text
worker/review_existing_uv.py
worker/app_uv_review_contract.py
tests/test_uv_review_contract.py
```

Tasks:

- `inspect_uv_layers` command 구현
- `review_existing_uv` command 구현
- `status.json` lifecycle 작성
- `uv_review_summary.json` normalization
- stdout/stderr log 저장
- Blender import/open 지원:
  - `.blend`
  - `.fbx`
  - `.obj`
  - `.glb/.gltf`

Acceptance:

- sample pottery FBX의 `UVChannel_1`을 review할 수 있다.
- no-UV fixture가 `status: no_uv`로 끝난다.
- 성공 run은 required JSON artifact를 모두 남긴다.

### Session C: UV Layout + Checker Artifacts

Owner files:

```text
uv_agent/blender/review_render.py
worker/review_existing_uv.py
tests/test_uv_review_artifacts.py
```

Tasks:

- `uv_layout.png` export 구현
- checker material 적용 구현
- checker front/side render 구현
- camera framing 안정화
- optional `checker_3q.png` 구현 여부 결정
- image artifact 존재/크기 smoke test

Acceptance:

- `uv_layout.png`, `checker_front.png`, `checker_side.png`가 생성된다.
- active UV layer가 없는 경우 checker render를 시도하지 않는다.
- 원본 model path를 덮어쓰지 않는다.

### Session D: Electron Main UV Review Service

Owner files:

```text
app/electron/main/uvReview*
app/shared/contracts/uvReview*
app/electron/main/project*
```

Tasks:

- IPC handlers:
  - `uv:inspectLayers`
  - `uv:setActiveLayer`
  - `uv:reviewExisting`
  - `uv:getReviewRun`
- project path resolve
- worker spawn/cancel
- artifact path normalization
- `project.json` MVP 1 extension 갱신

Acceptance:

- main process test에서 mock worker 결과를 project에 기록할 수 있다.
- selected UV layer가 project state에 저장된다.
- latest review run을 다시 열 수 있다.

### Session E: Renderer UV Review UI

Owner files:

```text
app/electron/renderer/uv-review/*
app/shared/contracts/uvReview*
```

Tasks:

- UV review workspace 구현
- object/UV layer selector
- checker tab
- UV layout tab with zoom/pan
- metric panel
- issue list
- no-UV empty state
- raw JSON report tab

Acceptance:

- 사용자가 project open -> inspect UV -> select layer -> review run -> report 확인을 완료할 수 있다.
- no-UV 상태에서도 다음 단계 안내가 보이고 app이 멈추지 않는다.
- metric text와 image가 작은 화면에서도 겹치지 않는다.

### Session F: QA Fixtures + E2E Smoke

Owner files:

```text
tests/e2e/test_mvp1_uv_review.py
sample/
docs/MVP1_QA_RESULTS.ko.md
```

Tasks:

- UV 있는 fixture와 no-UV fixture 준비
- Blender 없는 환경 skip 정책
- sample pottery smoke 결과 기록
- artifact existence와 JSON schema validation
- Electron main 또는 renderer smoke 작성

Acceptance:

- Blender가 없으면 tests skip으로 통과한다.
- Blender가 있으면 inspect/review smoke가 돈다.
- QA 결과 문서에 command, artifact path, metric summary, pass/fail이 남는다.

---

## 11. 구현 순서

권장 순서:

1. Blender UV layer -> `UVMap` adapter 구현
2. no-UV typed result 구현
3. `inspect_uv_layers` worker command 구현
4. `review_existing_uv` summary JSON contract 구현
5. UV layout PNG export 구현
6. checker front/side render 구현
7. Electron main IPC/service 구현
8. Renderer UV review workspace 구현
9. no-UV and failure UX 검증
10. sample pottery smoke와 QA 결과 문서 작성

세션 병렬화 기준:

- Session A/B/C는 worker contract를 공유하므로 `app_uv_review_contract.py`를 먼저 고정한다.
- Session E는 mock `uv_review_summary.json`으로 선개발 가능하다.
- Session D는 mock worker로 선개발 가능하다.
- Session F는 fixture와 expected schema를 먼저 만들고 실제 Blender smoke를 나중에 붙인다.

---

## 12. Production Acceptance Checklist

Functional:

- [ ] MVP 0 `project.json`에서 `working_model`을 찾을 수 있다.
- [ ] direct FBX/OBJ/GLB/GLTF low-poly import를 inspect할 수 있다.
- [ ] object list와 UV layer list가 UI에 표시된다.
- [ ] active UV layer를 선택할 수 있다.
- [ ] 기존 UV layer 기준 `uv_layout.png`가 생성된다.
- [ ] checker front render가 생성된다.
- [ ] checker side render가 생성된다.
- [ ] stretch/overlap/density/packing metrics가 UI에 표시된다.
- [ ] no-UV model이 `status: no_uv`로 처리된다.
- [ ] mandatory 90 rule이 MVP 1 report/gate에 나타나지 않는다.

Robustness:

- [ ] Blender executable path가 없으면 setup error를 보여준다.
- [ ] UV layer name이 잘못되면 structured error를 반환한다.
- [ ] worker failure가 앱 crash로 이어지지 않는다.
- [ ] image artifact 생성 실패는 warnings로 표시된다.
- [ ] stdout/stderr log가 run folder에 저장된다.
- [ ] 원본 working model을 덮어쓰지 않는다.

Contract:

- [ ] 모든 command는 JSON input/output을 갖는다.
- [ ] 모든 review run은 `status.json`을 갖는다.
- [ ] accepted review run은 `uv_review_summary.json`을 갖는다.
- [ ] Renderer는 stdout을 parse하지 않는다.
- [ ] artifact path는 project-relative로 summary에 저장된다.

Quality:

- [ ] Python tests 통과
- [ ] Electron typecheck 통과
- [ ] Electron renderer build 통과
- [ ] sample pottery UV review smoke 결과 문서화

---

## 13. 위험 요소와 대응

### 기존 UV island 추출이 생각보다 복잡함

UV seam/island는 mesh edge seam flag와 항상 일치하지 않는다. 같은 mesh vertex라도 loop UV가 다르면 UV island boundary일 수 있다.

대응:

- MVP 1에서는 seam flag가 아니라 loop UV discontinuity 기준으로 island를 추출한다.
- island 추출이 불완전해도 bounds/overlap/checker report는 생성되게 한다.
- island confidence 또는 warning을 summary에 남긴다.

### Blender UV layout export operator 의존성

`bpy.ops.uv.export_layout`는 edit mode/selection/context에 민감하다.

대응:

- worker에서 object activate -> edit mode -> select all -> export 순서를 고정한다.
- 실패 시 optional SVG fallback을 검토한다.
- export 실패는 worker crash가 아니라 artifact warning으로 처리한다.

### Checker render가 원본 asset을 오염시킬 위험

checker material을 적용한 scene을 저장하면 사용자의 material이 바뀔 수 있다.

대응:

- worker는 임시 Blender process에서만 material을 적용한다.
- 원본 file은 절대 save하지 않는다.
- 필요한 경우 copy scene/object에서 render한다.

### Metrics를 production gate로 오해할 위험

MVP 1 metric은 reviewer aid다. UV가 artist-quality인지 최종 판단하지 않는다.

대응:

- UI wording에서 "pass/fail"보다 "issues"와 "review status"를 쓴다.
- export 가능 여부는 MVP 5에서 별도 결정한다.
- AI식 단정 문구를 쓰지 않는다.

### 여러 UV layer 처리

FBX asset은 여러 UV channel을 가질 수 있다.

대응:

- active layer를 기본 선택한다.
- 사용자가 다른 layer를 선택할 수 있게 한다.
- review run은 반드시 `uv_layer`를 기록한다.

---

## 14. Handoff Notes for Other Sessions

작업 시작 전 확인:

```bash
git status --short
rg "UVMap|uv_layers|export_layout|checker|raster_overlap" uv_agent worker tests docs
```

다른 세션 규칙:

- 각 세션은 자신의 owner files만 수정한다.
- shared contract 변경은 먼저 이 문서의 Worker API Contract와 Project State Contract를 갱신한다.
- MVP 1 worker는 기존 UV를 수정하지 않는다.
- generated image/output은 git에 추가하지 않는다.
- `.context/`에는 smoke output과 임시 project를 저장해도 된다.
- 큰 sample asset은 git에 추가하지 말고 `.context/attachments` 또는 별도 download instruction으로 둔다.

권장 PR 설명:

```text
MVP1 adds read-only UV review for approved low-poly assets:
- inspects existing UV layers from the MVP0 working model
- generates UV layout and checker preview artifacts
- computes stretch, overlap, texel-density, packing, and bounds metrics
- exposes Electron IPC and renderer UI for UV review runs
- handles no-UV models without crashing
```

---

## 15. MVP 1 Done Definition

MVP 1은 다음 demo가 한 번에 성공하면 완료로 본다.

```text
1. App opens an MVP 0 project.
2. App finds project.json -> work/working_lowpoly.blend.
3. User opens UV Review workspace.
4. App lists mesh object and UV layers.
5. User selects UVChannel_1.
6. User runs Review UV.
7. App shows uv_layout.png.
8. App shows checker_front.png and checker_side.png.
9. App shows stretch, overlap, density, packing, island count, UV bounds.
10. App records latest_uv_review_run_id in project.json.
11. No-UV fixture shows a clear no-UV state instead of failing.
```

이 상태가 되면 MVP 2 User Seam Spec Editor 세션은 같은 project/object/UV layer context를 이어받아 edge selection과 existing UV boundary extraction을 시작할 수 있다.
