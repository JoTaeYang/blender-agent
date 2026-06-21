# Electron UV Review App MVP 2 Production Plan

> 기준 PRD: `docs/ELECTRON_UV_REVIEW_APP_PRD.ko.md`  
> 선행 계약: `docs/ELECTRON_UV_REVIEW_APP_MVP0_PRODUCTION_PLAN.ko.md`, `docs/ELECTRON_UV_REVIEW_APP_MVP1_PRODUCTION_PLAN.ko.md`  
> 범위: MVP 2 User Seam Spec Editor를 production-ready 단계로 끌어올리기 위한 구현 계획  
> 대상: 다른 Conductor 세션, Electron/Three.js 작업자, Python/Blender worker 작업자, seam spec/edge-id 작업자, QA 작업자  
> 핵심 목표: 사용자가 low-poly mesh의 edge를 정확히 선택해 seam/protect 상태를 지정하고, 기존 UV boundary를 seam spec으로 추출하며, MVP 3 generate/optimize가 그대로 사용할 수 있는 `user_seam_spec.json`을 저장한다.

---

## 1. MVP 2 정의

MVP 2는 UV를 생성하지 않는다. 사용자의 seam 의도를 저장하는 단계다.

MVP 2의 제품 완료 상태:

```text
MVP 1 selected object / UV layer
  -> editor용 edge geometry 로드
  -> edge select
  -> Mark Seam / Protect / Clear
  -> existing UV boundary를 seam으로 추출 optional
  -> seam/protect overlay 확인
  -> user_seam_spec.json 저장
  -> MVP 3 generate_uv input으로 handoff
```

MVP 2에서 반드시 보장할 것:

- UI edge id와 Blender worker edge id가 일치한다.
- 사용자가 edge를 선택할 수 있다.
- 선택 edge를 `normal`, `seam`, `protected` 상태로 변경할 수 있다.
- seam/protect 상태가 3D viewport에 overlay로 표시된다.
- `user_seam_spec.json` 저장/로드가 가능하다.
- 기존 UV layer가 있으면 UV island boundary를 seam spec으로 추출할 수 있다.
- 저장된 spec은 `artist_uv_agent.user_seams.UserSeamSpec`로 load 가능하다.
- invalid edge id, seam/protect conflict, object mismatch를 report한다.

MVP 2에서 하지 않을 것:

- UV unwrap/generate/pack/optimize
- AI/Nemotron 제안
- 자동 seam repair
- 사용자 승인 없는 seam 추가
- final production export
- full-featured Blender DCC 수준의 modeling edit

중요한 제품 원칙:

- 사용자가 만든 seam/protect가 source of truth다.
- 기존 UV boundary extraction은 사용자에게 보여주는 draft action이며, 저장은 사용자의 명시적 승인 후에만 한다.
- mandatory 90 rule은 MVP 2 editor의 기본 자동 seam 추가로 쓰지 않는다.
- MVP 3에서 사용될 기본 옵션은 `auto_refine_user_seams=false`, `repair_user_seams=false`, `enforce_user_mandatory=false`, `gate_user_mandatory=false`가 되도록 spec handoff를 설계한다.

---

## 2. MVP 1과의 연결

MVP 1 완료 후 `project.json`은 최소한 다음 값을 가질 수 있다.

```json
{
  "working_model": "work/working_lowpoly.blend",
  "selected_object": "SM_Test_Pottery_a_02",
  "selected_uv_layer": "UVChannel_1",
  "latest_uv_review_run_id": "review_run_uuid"
}
```

MVP 2는 기본적으로 다음 입력을 사용한다.

1. `working_model`
2. `selected_object`
3. `selected_uv_layer` optional
4. `latest_uv_review_run_id` optional

MVP 2 artifact는 `work/seams/`와 `runs/<seam_run_id>/`에 저장한다.

```text
<project>/
  work/
    seams/
      user_seam_spec.json
      reference_boundary_seam_spec.json
      drafts/
        draft_<timestamp>.json
  runs/
    <seam_run_id>/
      job.json
      status.json
      stdout.log
      stderr.log
      seam_editor_state.json
      edge_geometry.json
      seam_spec_validation.json
      boundary_extract_report.json
      overlay_preview.png optional
```

`project.json` MVP 2 extension:

```json
{
  "active_user_seam_spec": "work/seams/user_seam_spec.json",
  "latest_seam_editor_run_id": "seam_run_uuid",
  "seam_editor_runs": ["seam_run_uuid"]
}
```

---

## 3. 현재 코드 기반에서의 출발점

이미 존재하는 관련 자산:

```text
artist_uv_agent/user_seams.py
tests/test_user_seams.py
uv_agent/blender/extract.py
uv_agent/geometry/mesh_graph.py
```

재사용 가능한 기능:

- `UserSeamSpec` dataclass
- user seam spec JSON load/save
- invalid edge id detection
- `user_seam_edges` / `user_protected_edges` precedence
- chapters schema
- Blender object -> `MeshGraph` extraction

MVP 2에서 새로 고정해야 하는 것:

- editor용 mesh edge geometry export
- edge id stable mapping contract
- edge selection hit-test strategy
- seam/protect edit operation contract
- existing UV boundary -> `user_seam_edges` extraction
- seam editor state persistence
- spec validation worker

주의:

- `artist_uv_agent.user_seams.build_user_seam_set()`은 mandatory edge를 계산할 수 있지만, MVP 2 editor는 이를 자동 seam으로 저장하지 않는다.
- MVP 2 spec 저장은 현재 schema와 호환되어야 한다.
- 추가 metadata가 필요하면 spec root에 임의 필드를 넣기보다 별도 `seam_editor_state.json`에 둔다.

---

## 4. User Seam Spec Contract

MVP 3 handoff용 canonical spec:

```json
{
  "version": 1,
  "object": "SM_Test_Pottery_a_02",
  "mode": "user_seams",
  "mandatory_fold_angle": 90.0,
  "user_seam_edges": [16, 113, 138],
  "user_protected_edges": [20, 21],
  "chapters": [],
  "notes": "Authored in Electron MVP2"
}
```

Rules:

- `user_seam_edges`와 `user_protected_edges`는 integer Blender mesh edge ids다.
- 같은 edge가 seam과 protected에 동시에 들어가면 UI에서는 conflict로 보여주고 저장 시 기본 정책은 seam wins다.
- edge id는 현재 selected object의 evaluated mesh가 아니라 base mesh 기준이어야 한다.
- object name이 다르면 load는 가능하되 warning을 보여주고 apply는 막는다.
- edge id가 mesh range 밖이면 invalid로 표시하고 저장 시 제거 여부를 사용자에게 묻는다. Headless save command는 invalid를 제거하고 report에 남긴다.
- `chapters`는 MVP 2에서 optional이다. 기본은 빈 배열이다.

MVP 2 editor state는 spec과 분리한다.

```json
{
  "schema_version": 1,
  "object": "SM_Test_Pottery_a_02",
  "selected_edges": [16, 113],
  "hidden_edges": [],
  "view": {
    "camera": null,
    "overlay_mode": "seam_protect"
  },
  "draft_source": "manual | uv_boundary | loaded_spec",
  "last_saved_spec": "work/seams/user_seam_spec.json"
}
```

---

## 5. Edge Geometry Contract

MVP 2의 가장 큰 production risk는 UI edge id와 Blender edge id가 어긋나는 것이다. 따라서 worker가 editor용 edge geometry를 export하고 Renderer는 그 edge id를 그대로 사용한다.

### 5.1 `export_edge_geometry`

Input:

```json
{
  "command": "export_edge_geometry",
  "project_id": "project_uuid",
  "model": "/absolute/path/to/project/work/working_lowpoly.blend",
  "object_name": "SM_Test_Pottery_a_02",
  "out_dir": "/absolute/path/to/project/runs/seam_run_uuid"
}
```

Output:

```json
{
  "schema_version": 1,
  "status": "accepted",
  "object_name": "SM_Test_Pottery_a_02",
  "mesh_signature": {
    "vertices": 6562,
    "edges": 18701,
    "faces": 12152,
    "loops": 36396
  },
  "artifacts": {
    "edge_geometry": "edge_geometry.json"
  }
}
```

`edge_geometry.json`:

```json
{
  "schema_version": 1,
  "object": "SM_Test_Pottery_a_02",
  "vertices": [
    {"id": 0, "co": [0.0, 0.0, 0.0]},
    {"id": 1, "co": [1.0, 0.0, 0.0]}
  ],
  "edges": [
    {
      "id": 0,
      "vertex_ids": [0, 1],
      "face_ids": [0, 1],
      "is_boundary": false,
      "is_non_manifold": false,
      "is_sharp": false,
      "is_seam": false,
      "dihedral_angle": 12.5
    }
  ],
  "faces": [
    {
      "id": 0,
      "vertex_ids": [0, 1, 2],
      "edge_ids": [0, 1, 2],
      "material_index": 0
    }
  ]
}
```

Renderer rules:

- Renderer must not rebuild edge ids from imported GLTF/OBJ ordering.
- Renderer uses `edge_geometry.json.edges[].id` as the only selectable edge id.
- For performance, Renderer may convert JSON to typed arrays internally, but must preserve ids.
- Any mesh decimation/modifier/evaluated topology changes after this export invalidate the edge geometry and seam spec.

---

## 6. Worker API Contract

### 6.1 `load_user_seam_spec`

Input:

```json
{
  "command": "load_user_seam_spec",
  "project_id": "project_uuid",
  "path": "/absolute/path/to/project/work/seams/user_seam_spec.json",
  "model": "/absolute/path/to/project/work/working_lowpoly.blend",
  "object_name": "SM_Test_Pottery_a_02"
}
```

Output:

```json
{
  "schema_version": 1,
  "status": "accepted",
  "spec": {
    "version": 1,
    "object": "SM_Test_Pottery_a_02",
    "mode": "user_seams",
    "mandatory_fold_angle": 90.0,
    "user_seam_edges": [16, 113],
    "user_protected_edges": [20],
    "chapters": [],
    "notes": ""
  },
  "validation": {
    "valid": true,
    "invalid_edges": [],
    "conflicts": [],
    "object_mismatch": false
  }
}
```

### 6.2 `save_user_seam_spec`

Input:

```json
{
  "command": "save_user_seam_spec",
  "project_id": "project_uuid",
  "model": "/absolute/path/to/project/work/working_lowpoly.blend",
  "object_name": "SM_Test_Pottery_a_02",
  "spec": {
    "version": 1,
    "object": "SM_Test_Pottery_a_02",
    "mode": "user_seams",
    "mandatory_fold_angle": 90.0,
    "user_seam_edges": [16, 113],
    "user_protected_edges": [20],
    "chapters": [],
    "notes": "Manual seams"
  },
  "out_path": "/absolute/path/to/project/work/seams/user_seam_spec.json"
}
```

Output:

```json
{
  "schema_version": 1,
  "status": "accepted",
  "path": "work/seams/user_seam_spec.json",
  "validation": {
    "valid": true,
    "user_seam_count": 2,
    "user_protected_count": 1,
    "invalid_edges": [],
    "conflicts": []
  }
}
```

### 6.3 `validate_user_seam_spec`

Input:

```json
{
  "command": "validate_user_seam_spec",
  "model": "/absolute/path/to/project/work/working_lowpoly.blend",
  "object_name": "SM_Test_Pottery_a_02",
  "spec": {
    "version": 1,
    "object": "SM_Test_Pottery_a_02",
    "mode": "user_seams",
    "user_seam_edges": [16],
    "user_protected_edges": [16, 999999],
    "chapters": []
  }
}
```

Output:

```json
{
  "schema_version": 1,
  "status": "accepted",
  "validation": {
    "valid": false,
    "object_mismatch": false,
    "invalid_edges": [999999],
    "conflicts": [
      {
        "edge_id": 16,
        "type": "seam_and_protected",
        "resolution": "seam_wins"
      }
    ],
    "normalized_spec": {
      "version": 1,
      "object": "SM_Test_Pottery_a_02",
      "mode": "user_seams",
      "mandatory_fold_angle": 90.0,
      "user_seam_edges": [16],
      "user_protected_edges": [],
      "chapters": [],
      "notes": ""
    }
  }
}
```

### 6.4 `extract_uv_boundary_as_seams`

목적: 기존 UV island boundary를 `user_seam_edges`로 변환한다. MVP 2에서 가장 중요한 shortcut이다.

Input:

```json
{
  "command": "extract_uv_boundary_as_seams",
  "project_id": "project_uuid",
  "model": "/absolute/path/to/project/work/working_lowpoly.blend",
  "object_name": "SM_Test_Pottery_a_02",
  "uv_layer": "UVChannel_1",
  "out_path": "/absolute/path/to/project/work/seams/reference_boundary_seam_spec.json"
}
```

Output:

```json
{
  "schema_version": 1,
  "status": "accepted",
  "path": "work/seams/reference_boundary_seam_spec.json",
  "object_name": "SM_Test_Pottery_a_02",
  "uv_layer": "UVChannel_1",
  "user_seam_count": 1230,
  "user_protected_count": 0,
  "spec": {
    "version": 1,
    "object": "SM_Test_Pottery_a_02",
    "mode": "user_seams",
    "mandatory_fold_angle": 90.0,
    "user_seam_edges": [1, 2, 3],
    "user_protected_edges": [],
    "chapters": [],
    "notes": "Extracted from UV island boundaries: UVChannel_1"
  },
  "report": {
    "boundary_edge_count": 1230,
    "ambiguous_edges": [],
    "non_manifold_edges": [],
    "uv_layer_missing": false
  }
}
```

No-UV Output:

```json
{
  "schema_version": 1,
  "status": "no_uv",
  "path": null,
  "object_name": "SM_Test_Pottery_a_02",
  "uv_layer": "UVChannel_1",
  "warnings": ["UV layer not found or empty."]
}
```

Boundary extraction rule:

- For each mesh edge with two linked loops/faces, compare the UV coordinates at both endpoint vertices across adjacent face loops.
- If either endpoint has discontinuous UV coordinates across the edge, that mesh edge is a UV boundary seam.
- Mesh boundary edges may be included as seams for completeness but should be reported separately.
- Non-manifold edges should be reported; include them only when UV discontinuity can be determined safely.

---

## 7. Renderer Editing Contract

Edge states:

```text
normal
seam
protected
selected
hovered
invalid
conflict
```

User commands:

- select edge
- multi-select edges
- box/lasso select optional
- mark selected as seam
- mark selected as protected
- clear selected
- invert selection optional
- select all seams
- select all protected
- import from UV boundary
- load seam spec
- save seam spec
- discard draft

State transition rules:

- `normal -> seam`: add edge id to `user_seam_edges`
- `normal -> protected`: add edge id to `user_protected_edges`
- `seam -> protected`: remove from `user_seam_edges`, add to `user_protected_edges`
- `protected -> seam`: remove from `user_protected_edges`, add to `user_seam_edges`
- `seam/protected -> normal`: remove from both
- conflicting loaded specs show conflict overlay until normalized or explicitly saved

Keyboard shortcuts are optional for MVP 2, but UI controls must exist.

---

## 8. Electron MVP 2 UX

MVP 2 first screen is the seam editor workspace.

Required layout:

```text
Top Bar
  Open Project | Load Working Mesh | Extract UV Boundary | Save Spec | Next: Generate UV

Left Panel
  Project
  Objects
  UV Layers
  Seam Specs
  Edge Filters

Center
  3D View
  Edge overlay
  Seam/protect/selected highlights

Right Panel
  Selection
  Mark Seam / Protect / Clear buttons
  Counts
  Validation issues
  Spec metadata

Bottom Panel
  Edge id
  Job status
  Logs
  Raw spec preview
```

Required controls:

- icon or compact buttons for select mode, seam, protect, clear
- UV boundary extraction button
- Save Spec button
- Load Spec button
- visible counts:
  - selected edges
  - seam edges
  - protected edges
  - invalid edges
  - conflicts
- overlay toggles:
  - show seams
  - show protected
  - show mesh wire
  - show UV-boundary draft

Visual rules:

- seam and protected must be visually distinct.
- selected edge must be distinguishable from seam/protect.
- invalid/conflict edges must be impossible to miss.
- UI text must not overlap controls on narrow windows.
- Do not use marketing/landing-page layout.

Initial implementation choice:

- Use Three.js for viewport.
- Use `edge_geometry.json` line segments for selectable overlays.
- Hit testing may use ray-to-segment distance in screen space. Accuracy matters more than visual polish.
- If full 3D selection is risky, support a fallback edge-id search/input for production unblock.

---

## 9. Project State Contract

`project.json` MVP 2 extension:

```json
{
  "active_user_seam_spec": "work/seams/user_seam_spec.json",
  "latest_seam_editor_run_id": "seam_run_uuid",
  "seam_editor_runs": ["seam_run_uuid"]
}
```

`runs/<seam_run_id>/status.json`:

```json
{
  "schema_version": 1,
  "run_id": "seam_run_uuid",
  "command": "export_edge_geometry | extract_uv_boundary_as_seams | validate_user_seam_spec",
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

`work/seams/user_seam_spec.json` is the preferred explicit MVP 3 input. Renderer state is not the source of truth after save. If it is absent, MVP 3 may derive a seam spec from the selected UV layer boundary, so the Seam Editor is an optional (not required) step for assets that already carry UVs from another tool.

---

## 10. MVP 3 Handoff Contract

MVP 3 generate/optimize will receive:

```json
{
  "model": "work/working_lowpoly.blend",
  "object_name": "SM_Test_Pottery_a_02",
  "seam_spec": "work/seams/user_seam_spec.json",
  "options": {
    "auto_refine_user_seams": false,
    "repair_user_seams": false,
    "enforce_user_mandatory": false,
    "gate_user_mandatory": false,
    "optimize_layout": true
  }
}
```

MVP 2 done criteria for handoff:

- spec file exists
- spec object matches selected object
- all edge ids are valid for current mesh
- seam/protect conflict normalized or reported
- project.json points to `active_user_seam_spec`

---

## 11. 병렬 작업 분해

다른 세션에 나눠 맡길 때 아래 단위로 분리한다. 한 세션이 여러 영역의 파일을 동시에 소유하지 않게 한다.

### Session A: Edge Geometry Export + Stable IDs

Owner files:

```text
uv_agent/blender/edge_geometry.py
worker/seam_editor_worker.py
tests/test_edge_geometry_contract.py
```

Tasks:

- Blender object -> editor edge geometry JSON export
- mesh signature 계산
- edge id / vertex id / face id contract 고정
- `.blend`, `.fbx`, `.obj`, `.glb/.gltf` open/import 지원
- large mesh JSON size warning

Acceptance:

- sample pottery에서 edge count가 Blender mesh edge count와 일치한다.
- edge id가 `uv_agent.blender.extract.extract_mesh_graph()`의 id와 일치한다.
- JSON schema가 deterministic하다.

### Session B: UV Boundary Extraction

Owner files:

```text
uv_agent/blender/uv_boundary.py
worker/seam_editor_worker.py
tests/test_uv_boundary_extract.py
```

Tasks:

- specified UV layer의 loop UV 읽기
- UV discontinuity 기반 boundary edge 추출
- `extract_uv_boundary_as_seams` command 구현
- no-UV / missing layer 처리
- boundary report 작성

Acceptance:

- pottery `UVChannel_1`에서 reference boundary seam spec을 생성한다.
- no-UV fixture가 `status: no_uv`로 끝난다.
- 생성 spec이 `UserSeamSpec.from_dict()`로 load된다.

### Session C: Seam Spec Validation + Persistence

Owner files:

```text
artist_uv_agent/user_seams.py
worker/app_seam_spec_contract.py
worker/seam_editor_worker.py
tests/test_seam_spec_contract.py
```

Tasks:

- load/save/validate command 구현
- object mismatch 검증
- invalid edge 제거/보고 정책 구현
- seam/protected conflict normalize
- project-relative path 반환
- existing `tests/test_user_seams.py`와 호환 유지

Acceptance:

- invalid edge ids가 report된다.
- seam/protected 중복은 seam wins로 normalized된다.
- saved spec round-trip이 된다.

### Session D: Electron Main Seam Editor Service

Owner files:

```text
app/electron/main/seamEditor*
app/shared/contracts/seamEditor*
app/electron/main/project*
```

Tasks:

- IPC handlers:
  - `seam:exportEdgeGeometry`
  - `seam:loadSpec`
  - `seam:saveSpec`
  - `seam:validateSpec`
  - `seam:extractUvBoundary`
  - `seam:getEditorRun`
- project path resolve
- worker spawn/cancel
- project.json MVP 2 fields update
- artifact path normalization

Acceptance:

- mock worker로 edge geometry와 spec save flow가 project에 기록된다.
- active spec path가 project state에 저장된다.
- failed worker response가 UI에 전달된다.

### Session E: Three.js Edge Selection UI

Owner files:

```text
app/electron/renderer/seam-editor/*
app/shared/contracts/seamEditor*
```

Tasks:

- seam editor workspace 구현
- edge geometry 렌더링
- hover/select hit testing
- multi-select
- mark seam/protect/clear controls
- overlay colors/states
- counts/validation panel
- raw spec preview
- fallback edge-id input

Acceptance:

- 사용자가 edge를 선택하고 seam/protect/clear를 적용할 수 있다.
- UI state가 spec JSON으로 변환된다.
- loaded spec이 overlay에 반영된다.
- conflict/invalid 상태가 표시된다.

### Session F: UV Boundary Import UX

Owner files:

```text
app/electron/renderer/seam-editor/*
app/electron/main/seamEditor*
app/shared/contracts/seamEditor*
```

Tasks:

- Extract UV Boundary action
- boundary draft preview
- replace current spec / merge into current spec 선택 UX
- extraction report 표시
- save confirmation flow

Acceptance:

- 기존 UV boundary를 한 번의 command로 seam draft로 불러온다.
- 사용자가 저장 전 count와 warning을 볼 수 있다.
- 저장하면 active_user_seam_spec이 갱신된다.

### Session G: QA Fixtures + E2E Smoke

Owner files:

```text
tests/e2e/test_mvp2_seam_editor.py
sample/
docs/MVP2_QA_RESULTS.ko.md
```

Tasks:

- UV 있는 fixture와 no-UV fixture 준비
- simple mesh fixture로 known boundary edge set 검증
- Blender 없는 환경 skip 정책
- sample pottery boundary extraction smoke 기록
- Electron main/renderer smoke 작성

Acceptance:

- Blender가 없으면 tests skip으로 통과한다.
- Blender가 있으면 edge geometry export와 boundary extraction smoke가 돈다.
- QA 결과 문서에 command, artifact path, seam count, pass/fail이 남는다.

---

## 12. 구현 순서

권장 순서:

1. edge geometry export contract 구현
2. seam spec validation/load/save command 구현
3. UV boundary extraction command 구현
4. Electron main seam service mock flow 구현
5. Renderer seam editor shell 구현
6. edge overlay rendering 구현
7. hit testing and selection 구현
8. seam/protect/clear edit state 구현
9. UV boundary import UX 구현
10. MVP 3 handoff path를 project.json에 저장
11. QA smoke와 결과 문서 작성

세션 병렬화 기준:

- Session A/B/C는 worker contract를 공유하므로 `app_seam_spec_contract.py`를 먼저 고정한다.
- Session E는 mock `edge_geometry.json`으로 선개발 가능하다.
- Session D는 mock worker로 선개발 가능하다.
- Session F는 B와 D가 끝나기 전에도 mock extraction result로 UI를 만들 수 있다.
- Session G는 simple known mesh fixture부터 만들고 pottery smoke는 후반에 붙인다.

---

## 13. Production Acceptance Checklist

Functional:

- [ ] MVP 1 project에서 selected object를 seam editor로 열 수 있다.
- [ ] edge geometry를 export하고 UI에 표시할 수 있다.
- [ ] UI에서 edge를 선택할 수 있다.
- [ ] selected edge를 seam으로 mark할 수 있다.
- [ ] selected edge를 protected로 mark할 수 있다.
- [ ] selected edge를 normal로 clear할 수 있다.
- [ ] seam/protected counts가 표시된다.
- [ ] user seam spec을 저장할 수 있다.
- [ ] user seam spec을 다시 load할 수 있다.
- [ ] 기존 UV boundary를 seam spec으로 추출할 수 있다.
- [ ] saved spec이 MVP 3 input path로 project.json에 기록된다.

Robustness:

- [ ] Blender executable path가 없으면 setup error를 보여준다.
- [ ] object mismatch spec은 apply를 막고 warning을 보여준다.
- [ ] invalid edge ids는 crash 없이 report된다.
- [ ] edge geometry mesh signature mismatch가 감지된다.
- [ ] no-UV model에서 boundary extraction은 `status: no_uv`로 처리된다.
- [ ] worker failure가 앱 crash로 이어지지 않는다.
- [ ] generated output은 project folder에만 저장된다.

Contract:

- [ ] 모든 worker command는 JSON input/output을 갖는다.
- [ ] 모든 seam editor run은 `status.json`을 갖는다.
- [ ] `user_seam_spec.json`은 `UserSeamSpec.from_dict()`로 load된다.
- [ ] Renderer는 자체 edge id를 생성하지 않는다.
- [ ] artifact path는 project-relative로 summary에 저장된다.

Quality:

- [ ] Python tests 통과
- [ ] Electron typecheck 통과
- [ ] Electron renderer build 통과
- [ ] sample pottery boundary extraction smoke 결과 문서화

---

## 14. 위험 요소와 대응

### UI edge id와 Blender edge id 불일치

Three.js가 mesh를 import하거나 merge하면 Blender edge ordering과 달라질 수 있다.

대응:

- Renderer는 Blender worker가 export한 `edge_geometry.json`만 selectable source로 사용한다.
- GLTF/OBJ viewer mesh는 시각용으로만 쓰고, selectable overlay는 별도 line geometry로 둔다.
- mesh signature mismatch가 있으면 spec apply/save를 막는다.

### Raycast edge selection 정확도

얇거나 복잡한 mesh에서 edge hit testing이 불안정할 수 있다.

대응:

- screen-space ray-to-segment distance threshold를 사용한다.
- hover edge id를 status bar에 표시한다.
- fallback edge-id input/search를 제공한다.
- selection tolerance를 UI setting으로 둔다.

### UV boundary extraction ambiguity

UV layer의 loop data가 비정상적이거나 non-manifold edge가 있으면 boundary 판정이 애매할 수 있다.

대응:

- ambiguous/non-manifold edge를 report에 분리한다.
- 확실한 discontinuity만 seam으로 넣는다.
- ambiguous edge는 UI에서 warning overlay로 보여주고 사용자가 결정하게 한다.

### Spec schema drift

MVP 3 pipeline은 현재 `artist_uv_agent.user_seams.UserSeamSpec` schema를 기대한다.

대응:

- canonical `user_seam_spec.json`에는 현재 schema만 저장한다.
- UI-only metadata는 `seam_editor_state.json`에 저장한다.
- breaking change가 필요하면 이 문서와 `tests/test_user_seams.py`를 먼저 갱신한다.

### Mandatory 90 rule 재도입

기존 pipeline helper는 mandatory folds를 계산한다. MVP 2에서 이를 자동 저장하면 제품 원칙과 충돌한다.

대응:

- MVP 2 editor에서는 mandatory edges를 자동으로 `user_seam_edges`에 넣지 않는다.
- 필요하면 diagnostic overlay로만 표시한다.
- MVP 3 handoff options에서 mandatory gate/refine는 기본 false로 둔다.

---

## 15. Handoff Notes for Other Sessions

작업 시작 전 확인:

```bash
git status --short
rg "UserSeamSpec|user_seam_edges|user_protected_edges|uv boundary|edge_geometry" artist_uv_agent uv_agent worker tests docs
```

다른 세션 규칙:

- 각 세션은 자신의 owner files만 수정한다.
- shared contract 변경은 먼저 이 문서의 User Seam Spec Contract, Edge Geometry Contract, Worker API Contract를 갱신한다.
- MVP 2 worker는 UV를 생성하거나 수정하지 않는다.
- MVP 2 renderer는 Blender edge id를 새로 만들지 않는다.
- generated edge geometry, seam drafts, smoke outputs는 project folder 또는 `.context/`에 저장한다.
- 큰 sample asset은 git에 추가하지 말고 `.context/attachments` 또는 별도 download instruction으로 둔다.

권장 PR 설명:

```text
MVP2 adds user-authored seam spec editing:
- exports stable Blender edge geometry for renderer selection
- supports manual seam/protect/clear edge states
- extracts existing UV island boundaries into user seam specs
- validates and saves UserSeamSpec-compatible JSON
- records the active seam spec for MVP3 generate/optimize handoff
```

---

## 16. MVP 2 Done Definition

MVP 2는 다음 demo가 한 번에 성공하면 완료로 본다.

```text
1. App opens an MVP 1 project.
2. App finds project.json -> work/working_lowpoly.blend and selected object.
3. User opens Seam Editor workspace.
4. App exports and loads edge_geometry.json.
5. User selects an edge in the 3D view.
6. User marks it as seam.
7. User selects another edge and marks it as protected.
8. App shows seam/protected overlays and counts.
9. User extracts existing UV boundary from UVChannel_1.
10. App shows extracted seam count and draft warning/report.
11. User saves user_seam_spec.json.
12. project.json points to active_user_seam_spec.
13. Saved spec loads through artist_uv_agent.user_seams.UserSeamSpec.
14. MVP 3 can receive model + object_name + seam_spec path without additional conversion.
```

이 상태가 되면 MVP 3 Generate + Optimize 세션은 `active_user_seam_spec`을 source of truth로 사용해 user seam 기반 unwrap/pack/optimization 작업을 시작할 수 있다.
