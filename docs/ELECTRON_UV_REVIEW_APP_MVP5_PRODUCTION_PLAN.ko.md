# Electron UV Review App MVP 5 Production Plan

> 기준 PRD: `docs/ELECTRON_UV_REVIEW_APP_PRD.ko.md`  
> 선행 계약: `docs/ELECTRON_UV_REVIEW_APP_MVP0_PRODUCTION_PLAN.ko.md`, `docs/ELECTRON_UV_REVIEW_APP_MVP1_PRODUCTION_PLAN.ko.md`, `docs/ELECTRON_UV_REVIEW_APP_MVP2_PRODUCTION_PLAN.ko.md`, `docs/ELECTRON_UV_REVIEW_APP_MVP3_PRODUCTION_PLAN.ko.md`  
> 범위: MVP 4 AI Review를 건너뛰고 MVP 5 Production Export / History / Rollback을 production-ready 단계로 구현하기 위한 계획  
> 대상: 다른 Conductor 세션, Electron app 작업자, Python/Blender worker 작업자, export/QA 작업자  
> 핵심 목표: MVP 3의 accepted `selected_uv_model`을 FBX/OBJ/GLB/GLTF production asset으로 export하고, export history와 rollback 가능한 project state를 제공한다.

---

## 0. MVP 4를 건너뛰어도 되는가

가능하다.

MVP 4 AI Review는 report 설명과 제안 workflow다. Production export의 필수 technical dependency가 아니다. MVP 5가 의존해야 하는 것은 AI review 결과가 아니라 MVP 3의 accepted UV output이다.

MVP 5 진행 조건:

- `project.json.selected_uv_model`이 존재한다.
- `project.json.selected_uv_summary`가 존재한다.
- selected UV summary의 run status가 `accepted`다.
- `selected_uv_model`을 Blender에서 다시 열 수 있다.
- MVP 3 seam integrity와 overlap/bounds checks가 accepted 상태다.

MVP 4 skip policy:

- project manifest에 `ai_review_skipped: true` 또는 export record에 `ai_review_run_id: null`을 기록한다.
- UI는 “AI Review skipped”를 informational 상태로만 보여준다.
- export를 막는 gate로 쓰지 않는다.

---

## 1. MVP 5 정의

MVP 5는 final production asset export와 project history 단계다.

MVP 5의 제품 완료 상태:

```text
MVP 3 selected_uv_model
  -> export readiness check
  -> export options 선택
  -> FBX / OBJ / GLB 또는 GLTF export
  -> exported asset reopen validation
  -> export manifest 저장
  -> project history timeline에 연결
  -> 이전 selected UV / export result로 rollback 가능
```

MVP 5에서 반드시 보장할 것:

- accepted selected UV result만 export한다.
- FBX, OBJ, GLB/GLTF export를 지원한다.
- export result가 다시 열리는지 validation한다.
- export artifact와 source run/report/seam spec/candidate summary를 연결한다.
- export history가 project에 남는다.
- 사용자가 이전 accepted UV run 또는 export result로 rollback할 수 있다.
- source `working_model`, `user_seam_spec`, MVP 3 run artifacts를 덮어쓰지 않는다.

MVP 5에서 하지 않을 것:

- UV 재생성
- seam spec 수정
- candidate 재최적화
- AI review 실행
- texture baking
- cloud publish
- DCC-specific custom preset 전체 지원

---

## 2. MVP 3와의 연결

MVP 3 accepted project state:

```json
{
  "selected_uv_model": "work/uv/selected_uv.blend",
  "selected_uv_summary": "work/uv/selected_uv_summary.json",
  "latest_uv_generate_run_id": "uv_run_uuid",
  "active_user_seam_spec": "work/seams/user_seam_spec.json"
}
```

MVP 5 기본 입력:

1. `selected_uv_model`
2. `selected_uv_summary`
3. `latest_uv_generate_run_id`
4. `active_user_seam_spec`
5. `candidate_summary.json` from MVP 3 run
6. `p5_gate.json` and `seam_report.json` from MVP 3 run

MVP 5 output folder:

```text
<project>/
  exports/
    <export_id>/
      export_manifest.json
      status.json
      stdout.log
      stderr.log
      model.fbx optional
      model.obj optional
      model.mtl optional
      model.glb optional
      model.gltf optional
      textures/ optional
      validation_report.json
      uv_layout.png
      checker_front.png
      checker_side.png
  history/
    project_history.json
```

`project.json` MVP 5 extension:

```json
{
  "latest_export_id": "export_uuid",
  "exports": ["export_uuid"],
  "history": "history/project_history.json",
  "ai_review_skipped": true
}
```

---

## 3. 현재 코드 기반에서의 출발점

이미 존재하는 관련 자산:

```text
worker/generate_uv_from_seams.py
worker/run_quad_retopo_job.py
worker/run_app_retopo_job.py
uv_agent/blender/review_render.py
app/electron/main/project-service.ts
```

재사용 가능한 기능:

- MVP 3 selected UV handoff: `work/uv/selected_uv.blend`
- MVP 3 summary: `work/uv/selected_uv_summary.json`
- Blender export examples:
  - OBJ export with normals/UVs in `worker/run_quad_retopo_job.py`
  - FBX export in retopo workers
- checker/UV layout artifacts from MVP 1/3 render helpers
- project run registration pattern in Electron main

MVP 5에서 새로 고정해야 하는 것:

- app-facing export worker
- export manifest schema
- export validation worker path
- project history schema
- rollback command contract
- Electron export UI

---

## 4. Export Readiness Contract

### 4.1 `check_export_readiness`

Input:

```json
{
  "command": "check_export_readiness",
  "project_id": "project_uuid",
  "selected_uv_model": "/absolute/path/to/project/work/uv/selected_uv.blend",
  "selected_uv_summary": "/absolute/path/to/project/work/uv/selected_uv_summary.json"
}
```

Output:

```json
{
  "schema_version": 1,
  "status": "accepted",
  "ready": true,
  "selected_uv_model": "work/uv/selected_uv.blend",
  "source_uv_run_id": "uv_run_uuid",
  "checks": {
    "model_exists": true,
    "summary_exists": true,
    "uv_run_accepted": true,
    "raster_overlap_ok": true,
    "uv_bounds_ok": true,
    "seam_integrity_ok": true,
    "ai_review_required": false,
    "ai_review_skipped": true
  },
  "blocking_issues": [],
  "warnings": ["AI Review was skipped."]
}
```

Not-ready output:

```json
{
  "schema_version": 1,
  "status": "needs_input",
  "ready": false,
  "blocking_issues": [
    {
      "code": "missing_selected_uv_model",
      "message": "Run MVP 3 Generate + Optimize before export."
    }
  ],
  "warnings": []
}
```

---

## 5. Production Export Contract

### 5.1 `export_production_asset`

Input:

```json
{
  "command": "export_production_asset",
  "project_id": "project_uuid",
  "export_id": "export_uuid",
  "selected_uv_model": "/absolute/path/to/project/work/uv/selected_uv.blend",
  "selected_uv_summary": "/absolute/path/to/project/work/uv/selected_uv_summary.json",
  "object_name": "SM_Test_Pottery_a_02",
  "formats": ["fbx", "obj", "glb"],
  "options": {
    "selected_uv_layer": "AI_UV",
    "apply_scale": true,
    "include_materials": true,
    "include_normals": true,
    "copy_textures": false,
    "triangulate": false,
    "axis_forward": "-Z",
    "axis_up": "Y",
    "export_name": "SM_Test_Pottery_a_02_low_uv"
  },
  "out_dir": "/absolute/path/to/project/exports/export_uuid"
}
```

Output:

```json
{
  "schema_version": 1,
  "export_id": "export_uuid",
  "command": "export_production_asset",
  "status": "accepted",
  "source": {
    "selected_uv_model": "work/uv/selected_uv.blend",
    "selected_uv_summary": "work/uv/selected_uv_summary.json",
    "uv_generate_run_id": "uv_run_uuid",
    "seam_spec": "work/seams/user_seam_spec.json",
    "selected_candidate_id": "slim_concave_m002",
    "ai_review_run_id": null,
    "ai_review_skipped": true
  },
  "exports": {
    "fbx": "exports/export_uuid/SM_Test_Pottery_a_02_low_uv.fbx",
    "obj": "exports/export_uuid/SM_Test_Pottery_a_02_low_uv.obj",
    "glb": "exports/export_uuid/SM_Test_Pottery_a_02_low_uv.glb"
  },
  "validation": {
    "status": "accepted",
    "reopen": {
      "fbx": true,
      "obj": true,
      "glb": true
    },
    "uv_layers": ["AI_UV"],
    "selected_uv_layer": "AI_UV",
    "faces": 12152,
    "vertices": 6562,
    "has_uv": true,
    "has_normals": true
  },
  "artifacts": {
    "manifest": "export_manifest.json",
    "validation_report": "validation_report.json",
    "uv_layout": "uv_layout.png",
    "checker_front": "checker_front.png",
    "checker_side": "checker_side.png"
  },
  "warnings": []
}
```

Partial success output:

```json
{
  "schema_version": 1,
  "export_id": "export_uuid",
  "status": "partial",
  "exports": {
    "fbx": "exports/export_uuid/model.fbx",
    "obj": "exports/export_uuid/model.obj"
  },
  "failed_formats": [
    {
      "format": "glb",
      "code": "export_failed",
      "message": "Blender GLB export failed."
    }
  ],
  "warnings": ["GLB export failed; FBX and OBJ were validated."]
}
```

Failure policy:

- If all requested formats fail, status is `failed`.
- If at least one requested format exports and validates, status may be `partial`.
- UI must not hide partial failures.

---

## 6. Export Manifest Contract

`exports/<export_id>/export_manifest.json`:

```json
{
  "schema_version": 1,
  "export_id": "export_uuid",
  "created_at": "2026-06-20T00:00:00.000Z",
  "status": "accepted",
  "formats": ["fbx", "obj", "glb"],
  "options": {
    "selected_uv_layer": "AI_UV",
    "apply_scale": true,
    "include_materials": true,
    "include_normals": true,
    "copy_textures": false,
    "triangulate": false
  },
  "source": {
    "selected_uv_model": "work/uv/selected_uv.blend",
    "selected_uv_summary": "work/uv/selected_uv_summary.json",
    "uv_generate_run_id": "uv_run_uuid",
    "active_user_seam_spec": "work/seams/user_seam_spec.json",
    "candidate_summary": "runs/uv_run_uuid/candidate_summary.json",
    "p5_gate": "runs/uv_run_uuid/p5_gate.json",
    "seam_report": "runs/uv_run_uuid/seam_report.json",
    "ai_review_run_id": null,
    "ai_review_skipped": true
  },
  "metrics": {
    "stretch_score": 0.06866,
    "worst_island_distortion": 0.202999,
    "raster_overlap_ratio": 0.0,
    "texel_density_variance": 0.000002,
    "packing_efficiency": 0.591278
  },
  "files": {
    "fbx": "SM_Test_Pottery_a_02_low_uv.fbx",
    "obj": "SM_Test_Pottery_a_02_low_uv.obj",
    "glb": "SM_Test_Pottery_a_02_low_uv.glb",
    "uv_layout": "uv_layout.png",
    "checker_front": "checker_front.png",
    "checker_side": "checker_side.png"
  },
  "validation": "validation_report.json"
}
```

This manifest is the source of truth for export history UI.

---

## 7. Validation Requirements

Export validation is mandatory.

Validation steps:

1. Reopen each exported file in a fresh Blender background process or fresh scene.
2. Confirm at least one mesh object exists.
3. Confirm UV layer exists.
4. Confirm selected/exported UV layer is present when format supports layer naming.
5. Confirm face/vertex counts are within expected tolerance.
6. Confirm normals exist if `include_normals=true`.
7. Render or generate best-effort checker/UV layout preview for exported result.

Validation report:

```json
{
  "schema_version": 1,
  "status": "accepted",
  "formats": {
    "fbx": {
      "reopen_ok": true,
      "mesh_count": 1,
      "faces": 12152,
      "vertices": 6562,
      "uv_layers": ["AI_UV"],
      "has_uv": true,
      "has_normals": true,
      "warnings": []
    }
  }
}
```

Tolerance policy:

- OBJ/FBX/GLB may differ in object naming and material representation.
- Face count should not change unless `triangulate=true`.
- Vertex count may change due to format-specific splits; report it, do not automatically fail unless wildly different.
- Missing UV is always a hard failure.

---

## 8. Project History Contract

`history/project_history.json`:

```json
{
  "schema_version": 1,
  "events": [
    {
      "id": "event_uuid",
      "type": "export_created",
      "created_at": "2026-06-20T00:00:00.000Z",
      "export_id": "export_uuid",
      "uv_generate_run_id": "uv_run_uuid",
      "selected_candidate_id": "slim_concave_m002",
      "seam_spec": "work/seams/user_seam_spec.json",
      "manifest": "exports/export_uuid/export_manifest.json",
      "summary": {
        "formats": ["fbx", "obj", "glb"],
        "status": "accepted",
        "raster_overlap_ratio": 0.0,
        "packing_efficiency": 0.591278
      }
    }
  ]
}
```

History event types:

- `uv_selected`
- `export_created`
- `export_failed`
- `rollback_performed`

History rules:

- append-only
- do not delete older export manifests
- rollback creates a new history event rather than rewriting history

---

## 9. Rollback Contract

Rollback targets:

- previous selected UV run
- previous export result
- previous seam spec path

### 9.1 `list_rollback_targets`

Input:

```json
{
  "command": "list_rollback_targets",
  "project_id": "project_uuid"
}
```

Output:

```json
{
  "schema_version": 1,
  "status": "accepted",
  "targets": [
    {
      "id": "uv_run_uuid",
      "type": "uv_run",
      "created_at": "2026-06-20T00:00:00.000Z",
      "selected_uv_model": "runs/uv_run_uuid/selected_uv.blend",
      "selected_candidate_id": "slim_concave_m002",
      "metrics": {
        "raster_overlap_ratio": 0.0,
        "packing_efficiency": 0.591278
      }
    },
    {
      "id": "export_uuid",
      "type": "export",
      "created_at": "2026-06-20T00:00:00.000Z",
      "manifest": "exports/export_uuid/export_manifest.json",
      "formats": ["fbx", "obj", "glb"]
    }
  ]
}
```

### 9.2 `rollback_project_state`

Input:

```json
{
  "command": "rollback_project_state",
  "project_id": "project_uuid",
  "target_type": "uv_run",
  "target_id": "uv_run_uuid"
}
```

Output:

```json
{
  "schema_version": 1,
  "status": "accepted",
  "rolled_back_to": {
    "type": "uv_run",
    "id": "uv_run_uuid",
    "selected_uv_model": "work/uv/selected_uv.blend",
    "selected_uv_summary": "work/uv/selected_uv_summary.json"
  },
  "history_event": "event_uuid"
}
```

Rollback rules:

- Rollback updates project pointers, not historical artifacts.
- Rollback to UV run copies that run’s selected UV blend/summary back into `work/uv/`.
- Rollback to export sets `latest_export_id`, but does not delete newer exports.
- Rollback requires user confirmation in UI.

---

## 10. Electron MVP 5 UX

MVP 5 first screen is Export workspace.

Required layout:

```text
Top Bar
  Open Project | Check Export | Export | History | Rollback

Left Panel
  Project
  Selected UV
  Export History
  Rollback Targets

Center
  Export Status
  Exported Files
  Validation Results
  Preview: checker / UV layout

Right Panel
  Export Options
  Source Links
  Metrics Snapshot
  Warnings

Bottom Panel
  Logs
  Raw Manifest
  Raw Validation Report
```

Required interactions:

- readiness check
- select export formats
- configure export options
- start export
- inspect validation results
- reveal exported files
- open export manifest
- list history
- rollback with confirmation

Export options UI:

- format checkboxes: FBX, OBJ, GLB/GLTF
- selected UV layer selector
- apply scale toggle
- include materials toggle
- include normals toggle
- copy textures toggle
- triangulate toggle
- export name input

UI wording:

- If MVP 4 was skipped, show “AI Review skipped” as a warning/info line, not a blocker.
- Do not say “production-ready” unless export validation passed for at least one requested format.
- Partial export must clearly show which formats failed.

---

## 11. Worker Implementation Strategy

Recommended worker:

```text
worker/export_production_asset.py
worker/app_export_contract.py
```

Worker responsibilities:

- parse job JSON
- open `selected_uv_model`
- resolve object and active UV layer
- apply export options in a temporary scene
- export requested formats
- reopen exported files for validation
- generate validation report
- generate UV/checker previews
- write `export_manifest.json`
- write `status.json`

Blender export operators:

- FBX: `bpy.ops.export_scene.fbx`
- OBJ: `bpy.ops.wm.obj_export` when available, fallback to `bpy.ops.export_scene.obj`
- GLB/GLTF: `bpy.ops.export_scene.gltf`

Do not:

- save changes back into `selected_uv_model`
- mutate `working_model`
- mutate `user_seam_spec`
- delete prior exports

---

## 12. 병렬 작업 분해

다른 세션에 나눠 맡길 때 아래 단위로 분리한다. 한 세션이 여러 영역의 파일을 동시에 소유하지 않게 한다.

### Session A: Export Worker Contract

Owner files:

```text
worker/app_export_contract.py
worker/export_production_asset.py
tests/test_export_contract.py
```

Tasks:

- `check_export_readiness` schema 구현
- `export_production_asset` schema 구현
- status lifecycle 작성
- export manifest normalization
- partial/failure result policy 구현

Acceptance:

- missing selected UV는 `needs_input`이 된다.
- accepted export는 `export_manifest.json`을 남긴다.
- partial export가 structured result로 표현된다.

### Session B: Blender Export Formats

Owner files:

```text
worker/export_production_asset.py
uv_agent/blender/export.py
tests/test_export_formats.py
```

Tasks:

- FBX export 구현
- OBJ export 구현
- GLB/GLTF export 구현
- selected UV layer activation
- apply scale / normals / materials / triangulate options 연결
- texture copy는 best-effort warning 처리

Acceptance:

- at least one small fixture exports to FBX/OBJ/GLB.
- requested UV data is present after export.
- source selected UV blend is not modified.

### Session C: Export Validation

Owner files:

```text
worker/export_production_asset.py
uv_agent/blender/export_validation.py
tests/test_export_validation.py
```

Tasks:

- exported file reopen validation
- UV presence validation
- face/vertex count snapshot
- normals/material warnings
- validation report 작성
- preview artifact generation

Acceptance:

- exported OBJ/FBX/GLB can be reopened in Blender.
- missing UV is hard failure.
- validation_report.json is UI-friendly.

### Session D: Project History + Rollback Service

Owner files:

```text
app/electron/main/exportHistory*
app/electron/main/project-service.ts
app/shared/contracts/export*
```

Tasks:

- history append helper
- export registration helper
- `list_rollback_targets`
- `rollback_project_state`
- project.json MVP 5 fields update
- no deletion of historical artifacts

Acceptance:

- export creates a history event.
- rollback creates a new history event.
- rollback restores `selected_uv_model` pointers without deleting newer exports.

### Session E: Electron Main Export IPC

Owner files:

```text
app/electron/main/export*
app/shared/contracts/export*
app/electron/main/ipc.ts
```

Tasks:

- IPC handlers:
  - `export:checkReadiness`
  - `export:start`
  - `export:getStatus`
  - `export:getManifest`
  - `export:listHistory`
  - `export:listRollbackTargets`
  - `export:rollback`
- worker spawn/cancel
- artifact path normalization
- reveal file support

Acceptance:

- mock worker export updates project state.
- failed export returns structured error.
- manifest and validation report can be reopened by UI.

### Session F: Renderer Export UI

Owner files:

```text
app/electron/renderer/export/*
app/shared/contracts/export*
```

Tasks:

- Export workspace 구현
- readiness panel
- format/options controls
- export progress/logs
- validation results view
- exported file links
- history timeline
- rollback confirmation UI

Acceptance:

- user can check readiness -> export -> inspect validation -> reveal files.
- AI skipped state is visible but non-blocking.
- partial export clearly displays failed formats.
- rollback flow requires confirmation.

### Session G: QA Fixtures + E2E Smoke

Owner files:

```text
tests/e2e/test_mvp5_export.py
sample/
docs/MVP5_QA_RESULTS.ko.md
```

Tasks:

- selected UV fixture setup
- FBX/OBJ/GLB export smoke
- reopen validation smoke
- rollback smoke
- Blender 없는 환경 skip 정책
- QA result doc 작성

Acceptance:

- Blender가 없으면 tests skip으로 통과한다.
- Blender가 있으면 at least OBJ + one binary format export smoke가 돈다.
- QA 문서에 exported file paths, validation result, rollback result가 남는다.

---

## 13. 구현 순서

권장 순서:

1. `app_export_contract.py` 작성
2. `check_export_readiness` 구현
3. `export_production_asset` worker skeleton 구현
4. OBJ export + validation 먼저 구현
5. FBX export 추가
6. GLB/GLTF export 추가
7. export manifest 작성
8. project history append 구현
9. rollback service 구현
10. Electron main IPC 구현
11. Renderer Export workspace 구현
12. QA smoke와 결과 문서 작성

세션 병렬화 기준:

- Session A가 export manifest/status contract를 먼저 고정한다.
- Session F는 mock manifest/validation report로 선개발 가능하다.
- Session D는 worker 없이 mock export records로 개발 가능하다.
- Session C는 Session B의 최소 OBJ export 후 붙인다.
- Session G는 readiness/rollback tests를 먼저 만들고 Blender export smoke를 후반에 붙인다.

---

## 14. Production Acceptance Checklist

Functional:

- [ ] MVP 3 selected UV result가 없으면 export가 막힌다.
- [ ] AI Review skipped 상태가 blocker가 아니다.
- [ ] FBX export가 가능하다.
- [ ] OBJ export가 가능하다.
- [ ] GLB 또는 GLTF export가 가능하다.
- [ ] exported file reopen validation이 실행된다.
- [ ] UV layer가 export 결과에 남아 있다.
- [ ] export_manifest.json이 생성된다.
- [ ] project history에 export event가 추가된다.
- [ ] 이전 UV run/export로 rollback할 수 있다.

Robustness:

- [ ] source working model을 덮어쓰지 않는다.
- [ ] selected UV blend를 export 중 오염시키지 않는다.
- [ ] 일부 format 실패가 전체 app crash로 이어지지 않는다.
- [ ] stdout/stderr log가 export folder에 저장된다.
- [ ] old exports are not deleted by new exports or rollback.

Contract:

- [ ] 모든 export command는 JSON input/output을 갖는다.
- [ ] 모든 export는 status.json을 갖는다.
- [ ] accepted/partial export는 export_manifest.json을 갖는다.
- [ ] validation_report.json은 format별 reopen result를 포함한다.
- [ ] project.json latest_export_id가 accepted/partial export를 가리킨다.

Quality:

- [ ] Python tests 통과
- [ ] Electron typecheck 통과
- [ ] Electron renderer build 통과
- [ ] sample export smoke 결과 문서화

---

## 15. 위험 요소와 대응

### Blender export format 차이

FBX/OBJ/GLB는 UV layer naming, material, normals 처리 방식이 다르다.

대응:

- format별 validation report를 분리한다.
- object/material naming 차이는 warning으로 처리한다.
- missing UV만 hard failure로 둔다.

### selected UV blend 오염 위험

Export option 적용 중 material/scale/triangulate가 source blend에 남을 수 있다.

대응:

- worker는 임시 scene/process에서 열고 export한다.
- export 후 source blend를 save하지 않는다.
- destructive modifiers는 duplicate object에만 적용한다.

### Rollback이 history를 덮어쓸 위험

Rollback이 단순 file overwrite로 구현되면 추적이 어려워진다.

대응:

- rollback은 project pointers와 `work/uv` handoff copy만 바꾼다.
- history에는 `rollback_performed` event를 append한다.
- original export folders/runs are immutable.

### MVP 4 skip이 품질 gate로 오해될 위험

AI Review는 skipped지만 export validation은 반드시 필요하다.

대응:

- UI에 AI Review skipped를 정보성 warning으로 표시한다.
- export readiness checks는 MVP 3 metrics와 export validation에만 의존한다.
- manifest에 `ai_review_skipped: true`를 기록한다.

---

## 16. Handoff Notes for Other Sessions

작업 시작 전 확인:

```bash
git status --short
rg "selected_uv_model|selected_uv_summary|export_scene|obj_export|history|rollback" app worker uv_agent docs tests
```

다른 세션 규칙:

- 각 세션은 자신의 owner files만 수정한다.
- shared contract 변경은 먼저 이 문서의 Export Contract, Manifest Contract, History/Rollback Contract를 갱신한다.
- MVP 5 worker는 UV를 재생성하지 않는다.
- MVP 5 worker는 source `working_model`, `selected_uv_model`, `user_seam_spec`을 overwrite하지 않는다.
- generated exports는 project `exports/` 또는 `.context/`에 저장한다.
- 큰 exported sample asset은 git에 추가하지 않는다.

권장 PR 설명:

```text
MVP5 adds production export, history, and rollback without requiring MVP4 AI Review:
- checks accepted MVP3 selected UV readiness
- exports FBX/OBJ/GLB or GLTF with UVs and validation
- writes export manifests tied to seam spec, candidate summary, and UV metrics
- appends project history events
- supports rollback to previous UV runs or exports
```

---

## 17. MVP 5 Done Definition

MVP 5는 다음 demo가 한 번에 성공하면 완료로 본다.

```text
1. App opens an MVP 3 project.
2. App finds project.json -> selected_uv_model and selected_uv_summary.
3. App shows AI Review skipped as non-blocking.
4. User opens Export workspace.
5. App passes export readiness check.
6. User selects FBX, OBJ, and GLB.
7. Worker exports requested formats.
8. Worker reopens exported files and validates UV presence.
9. App shows exported files and validation results.
10. export_manifest.json links source UV run, seam spec, candidate summary, metrics, and files.
11. project history records export_created.
12. User rolls back to a previous UV run or export after confirmation.
13. project history records rollback_performed.
```

이 상태가 되면 앱은 MVP 4 없이도 high-poly/low-poly preparation, UV review, seam spec editing, UV generation/optimization, and production export를 end-to-end로 수행할 수 있다.
