# Electron UV Review App MVP 3 Production Plan

> 기준 PRD: `docs/ELECTRON_UV_REVIEW_APP_PRD.ko.md`  
> 선행 계약: `docs/ELECTRON_UV_REVIEW_APP_MVP0_PRODUCTION_PLAN.ko.md`, `docs/ELECTRON_UV_REVIEW_APP_MVP1_PRODUCTION_PLAN.ko.md`, `docs/ELECTRON_UV_REVIEW_APP_MVP2_PRODUCTION_PLAN.ko.md`  
> 관련 구현 계획: `docs/UV_LAYOUT_OPTIMIZATION_LOOP_PLAN.ko.md`, `docs/USER_GUIDED_SEAM_UV_PIPELINE_PLAN.ko.md`  
> 범위: MVP 3 Generate + Optimize를 production-ready 단계로 끌어올리기 위한 구현 계획  
> 대상: 다른 Conductor 세션, Electron app 작업자, Python/Blender worker 작업자, UV pipeline 작업자, QA 작업자  
> 핵심 목표: MVP 2의 `active_user_seam_spec`을 source of truth로 사용해 UV unwrap/relax/pack 후보를 생성하고, seam set을 바꾸지 않은 상태에서 best candidate를 선택해 비교 가능한 artifacts를 저장한다.

---

## 1. MVP 3 정의

MVP 3는 user seam 기반 UV generation과 layout optimization 단계다.

MVP 3의 제품 완료 상태:

```text
seam source resolve
  -> active_user_seam_spec 있으면 사용
  -> 없으면 active UV layer boundary에서 derived seam spec 생성
  -> seam source validation
  -> user/reference seam 기반 unwrap
  -> baseline UV 생성
  -> relax / scale / rotate / pack 후보 탐색
  -> overlap/stretch/density/packing metric 평가
  -> best candidate 선택
  -> before/after checker + UV layout 비교
  -> selected UV를 project working result로 저장
  -> MVP 4 AI review 또는 MVP 5 export로 handoff
```

MVP 3에서 반드시 보장할 것:

- `active_user_seam_spec`이 없더라도 selected/active UV layer가 있으면 UV island boundary를 derived seam spec으로 추출해 실행한다.
- `active_user_seam_spec`도 없고 usable UV layer도 없을 때만 setup error(`needs_input`)를 보여준다.
- seam source(spec 또는 derived)의 edge id가 현재 mesh와 맞는지 validate한다.
- user/reference seam mode 기본 옵션은 다음이다.

```text
auto_refine_user_seams = false
repair_user_seams = false
enforce_user_mandatory = false
gate_user_mandatory = false
optimize_layout = true
layout_opt_preset = user_reference
layout_opt_max_candidates = 24
```

- seam set이 자동으로 변경되지 않는다.
- `auto_added_seams == 0`을 기본 acceptance로 둔다.
- `final_seam_count == user_seam_count`를 기본 acceptance로 둔다.
- candidate list가 생성되고 selected candidate가 기록된다.
- selected candidate의 UV layout/checker/report artifacts가 저장된다.
- overlap이 있으면 selected candidate로 ship하지 않는다.
- mandatory 90 audits는 report-only diagnostic으로만 남긴다.

MVP 3에서 하지 않을 것:

- 새로운 seam/chapter 자동 생성
- user seam spec overwrite
- protected edge 해제
- AI/Nemotron review
- final FBX/OBJ/GLB production export
- texture baking
- artist 판단 없이 “production-ready” 단정

---

## 2. MVP 2와의 연결

MVP 2 완료 후 `project.json`은 최소한 다음 값을 가진다.

```json
{
  "working_model": "work/working_lowpoly.blend",
  "selected_object": "SM_Test_Pottery_a_02",
  "active_user_seam_spec": "work/seams/user_seam_spec.json"
}
```

MVP 3는 다음 입력을 기본으로 사용한다.

1. `working_model`
2. `selected_object`
3. `active_user_seam_spec` optional
4. `selected_uv_layer` optional, fallback seam source용 (없으면 before/after comparison에도 사용)
5. `latest_uv_review_run_id` optional

MVP 3 run folder:

```text
<project>/
  work/
    uv/
      selected_uv.blend
      selected_uv.fbx optional
      selected_uv_summary.json
  runs/
    <uv_run_id>/
      job.json
      status.json
      stdout.log
      stderr.log
      uv_generate_summary.json
      seam_source_resolution.json
      derived_from_uv_boundary.json optional
      p5_gate.json
      seam_report.json
      candidate_summary.json
      baseline_uv_layout.png
      baseline_checker_front.png
      baseline_checker_side.png
      selected_uv_layout.png
      selected_checker_front.png
      selected_checker_side.png
      candidate_previews/
        <candidate_id>_uv_layout.png optional
        <candidate_id>_checker_front.png optional
```

`project.json` MVP 3 extension:

```json
{
  "latest_uv_generate_run_id": "uv_run_uuid",
  "uv_generate_runs": ["uv_run_uuid"],
  "selected_uv_model": "work/uv/selected_uv.blend",
  "selected_uv_summary": "work/uv/selected_uv_summary.json",
  "latest_derived_seam_spec": "work/seams/derived_from_uv_boundary.json"
}
```

---

## 3. 현재 코드 기반에서의 출발점

이미 존재하는 관련 자산:

```text
artist_uv_agent/user_seams.py
chart_uv_agent/pipeline.py
chart_uv_agent/unwrap.py
chart_uv_agent/layout_optimization.py
artist_uv_agent/seam_report.py
uv_agent/geometry/evaluation.py
worker/run_quad_retopo_job.py
tests/test_uv_layout_optimization.py
tests/test_user_seams.py
```

재사용 가능한 기능:

- `UserSeamSpec` load/validate
- `run_chart_uv(..., user_seam_spec=...)`
- strict user/reference mode flags
- layout optimization candidate sweep
- `p5_gate.json` / `seam_report.json` generation
- checker and UV layout rendering helpers from prior MVPs

MVP 3에서 새로 고정해야 하는 것:

- Electron용 `generate_uv_from_seams` wrapper
- project-local run/status/artifact contract
- candidate summary normalization
- baseline vs selected comparison artifacts
- selected UV model persistence
- UI candidate table and before/after view

주의:

- `worker/run_quad_retopo_job.py`는 existing P5 기능을 많이 갖고 있지만 app contract로 바로 노출하기엔 phase-specific이다.
- MVP 3 production path는 새 app worker wrapper를 만들고 내부에서 기존 chart pipeline을 호출하거나 기존 worker를 subprocess로 감싼다.
- Renderer는 `p5_gate.json` raw structure에 직접 의존하지 않고 `uv_generate_summary.json`을 primary input으로 사용한다.

---

## 4. Generate UV Contract

### 4.1 `generate_uv_from_seams`

Input:

```json
{
  "command": "generate_uv_from_seams",
  "project_id": "project_uuid",
  "run_id": "uv_run_uuid",
  "model": "/absolute/path/to/project/work/working_lowpoly.blend",
  "object_name": "SM_Test_Pottery_a_02",
  "seam_spec": "/absolute/path/to/project/work/seams/user_seam_spec.json",
  "uv_layer": "UVChannel_1",
  "seam_source_policy": "prefer_spec_then_uv_boundary",
  "options": {
    "uv_engine": "chart",
    "auto_refine_user_seams": false,
    "repair_user_seams": false,
    "enforce_user_mandatory": false,
    "gate_user_mandatory": false,
    "optimize_layout": true,
    "layout_opt_preset": "user_reference",
    "layout_opt_max_candidates": 24,
    "render_previews": true,
    "save_selected_blend": true
  },
  "out_dir": "/absolute/path/to/project/runs/uv_run_uuid"
}
```

Seam source resolution (`seam_source_policy = prefer_spec_then_uv_boundary`):

- `seam_spec`이 있으면 기존처럼 그것을 source of truth로 사용한다 (`user_seam_spec`).
- `seam_spec`이 없고 `uv_layer`가 있으면 UV island boundary에서 derived seam spec을 만들어 `work/seams/derived_from_uv_boundary.json`에 저장하고 사용한다 (`uv_boundary_derived`).
- `seam_spec`도 없고 usable `uv_layer`도 없으면 `status = needs_input`.

Output:

```json
{
  "schema_version": 1,
  "run_id": "uv_run_uuid",
  "command": "generate_uv_from_seams",
  "status": "accepted",
  "model": "work/working_lowpoly.blend",
  "object_name": "SM_Test_Pottery_a_02",
  "seam_spec": "work/seams/user_seam_spec.json",
  "seam_source": {
    "type": "user_seam_spec",
    "path": "work/seams/user_seam_spec.json",
    "uv_layer": null,
    "user_confirmed": true,
    "derived": false
  },
  "selected_candidate_id": "slim_concave_m002",
  "selected_uv_model": "work/uv/selected_uv.blend",
  "metrics": {
    "stretch_score": 0.06866,
    "worst_island_distortion": 0.202999,
    "raster_overlap_ratio": 0.0,
    "overlap_ratio": 0.0,
    "texel_density_variance": 0.000002,
    "packing_efficiency": 0.591278,
    "island_count": 52,
    "uv_bounds_ok": true
  },
  "seam_integrity": {
    "user_seam_count": 1230,
    "user_protected_count": 0,
    "final_seam_count": 1230,
    "auto_added_seams": 0,
    "mandatory_rule_enabled": false,
    "mandatory_gate_enabled": false,
    "valid": true
  },
  "layout_optimization": {
    "enabled": true,
    "selected_candidate_id": "slim_concave_m002",
    "kept_baseline": false,
    "candidate_count": 24,
    "score_before": -0.0031,
    "score_after": -0.003276,
    "packing_efficiency_before": 0.583109,
    "packing_efficiency_after": 0.591278,
    "stretch_before": 0.06866,
    "stretch_after": 0.06866
  },
  "artifacts": {
    "summary": "uv_generate_summary.json",
    "p5_gate": "p5_gate.json",
    "seam_report": "seam_report.json",
    "candidate_summary": "candidate_summary.json",
    "baseline_uv_layout": "baseline_uv_layout.png",
    "baseline_checker_front": "baseline_checker_front.png",
    "baseline_checker_side": "baseline_checker_side.png",
    "selected_uv_layout": "selected_uv_layout.png",
    "selected_checker_front": "selected_checker_front.png",
    "selected_checker_side": "selected_checker_side.png",
    "selected_blend": "selected_uv.blend"
  },
  "warnings": []
}
```

Failure output:

```json
{
  "schema_version": 1,
  "run_id": "uv_run_uuid",
  "command": "generate_uv_from_seams",
  "status": "failed",
  "error": {
    "code": "invalid_seam_spec",
    "message": "Seam spec contains edge ids that do not exist on the selected mesh.",
    "details": {
      "invalid_edges": [999999]
    }
  },
  "artifacts": {
    "stdout": "stdout.log",
    "stderr": "stderr.log"
  }
}
```

UV boundary fallback일 때 `seam_source`는 derived로 기록된다.

```json
{
  "status": "accepted",
  "seam_spec": "work/seams/derived_from_uv_boundary.json",
  "seam_source": {
    "type": "uv_boundary_derived",
    "path": "work/seams/derived_from_uv_boundary.json",
    "uv_layer": "UVChannel_1",
    "user_confirmed": false,
    "derived": true
  }
}
```

Seam source가 없을 때 (`needs_input`):

```json
{
  "status": "needs_input",
  "error": {
    "code": "missing_seam_source",
    "message": "No user seam spec or usable UV layer was found. Select a UV layer or create seams."
  }
}
```

### 4.2 `select_uv_candidate`

MVP 3 기본은 worker가 best candidate를 자동 선택한다. 수동 candidate selection은 production MVP 3.1로 미뤄도 되지만, contract는 미리 둔다.

Input:

```json
{
  "command": "select_uv_candidate",
  "project_id": "project_uuid",
  "run_id": "uv_run_uuid",
  "candidate_id": "slim_concave_m002"
}
```

Output:

```json
{
  "status": "accepted",
  "selected_candidate_id": "slim_concave_m002",
  "selected_uv_model": "work/uv/selected_uv.blend"
}
```

Implementation note:

- MVP 3.0에서는 read-only placeholder로 두고 UI에서 disabled 처리 가능하다.
- 실제 manual reselection을 지원하려면 candidate별 UV state를 저장해야 한다. 현재 candidate sweep은 object UV를 덮어쓰므로 selected spec을 재적용하는 방식이 안전하다.

---

## 5. Candidate Summary Contract

`candidate_summary.json`:

```json
{
  "schema_version": 1,
  "baseline_candidate_id": "slim_concave_m005",
  "selected_candidate_id": "slim_concave_m002",
  "kept_baseline": false,
  "score_weights": {
    "stretch_score": 4.0,
    "worst_island_distortion": 3.0,
    "texel_density_variance": 2.0,
    "raster_overlap_ratio": 2.0,
    "overlap_ratio": 1.0,
    "packing_efficiency": -1.5,
    "small_island_ratio": 0.2
  },
  "candidates": [
    {
      "id": "slim_concave_m002",
      "unwrap_method": "MINIMUM_STRETCH",
      "minimize_iters": 0,
      "margin": 0.002,
      "pack_shape": "CONCAVE",
      "rotate": true,
      "average_scale": true,
      "accepted": true,
      "reason": "best_score",
      "score": -0.003276,
      "metrics": {
        "stretch_score": 0.06866,
        "worst_island_distortion": 0.202999,
        "raster_overlap_ratio": 0.0,
        "overlap_ratio": 0.0,
        "texel_density_variance": 0.000002,
        "packing_efficiency": 0.591278,
        "uv_bounds_ok": true
      }
    }
  ],
  "rejected": [
    {
      "id": "abf_aabb_m010_min30",
      "reason": "raster_overlap"
    }
  ]
}
```

UI는 candidate table을 이 파일에서 그린다. Raw `p5_gate.json`은 debug tab에서만 사용한다.

---

## 6. Seam Integrity Requirements

MVP 3의 핵심 acceptance는 seam set 보존이다.

Required checks:

- saved spec loads successfully
- spec object matches selected object
- invalid edge ids count is 0
- `auto_refine_user_seams == false`
- `repair_user_seams == false`
- `enforce_user_mandatory == false`
- `gate_user_mandatory == false`
- `auto_added_seams == 0`
- `final_seam_count == user_seam_count`
- no protected edge is present in final seam set unless also explicitly marked seam by user and normalized by MVP 2

If seam integrity fails:

- status should be `failed` or `needs_user_review`
- selected UV model must not replace `work/uv/selected_uv.blend`
- UI should send user back to MVP 2 Seam Editor

Mandatory diagnostics:

- `mandatory_90_missing`, `mandatory_90_uv_unsplit`, and related audits may appear in raw reports.
- They must not gate-fail user/reference mode.
- UI may show them under “Diagnostics” only.

---

## 7. Image Artifact Requirements

Required comparison artifacts:

```text
baseline_uv_layout.png
baseline_checker_front.png
baseline_checker_side.png
selected_uv_layout.png
selected_checker_front.png
selected_checker_side.png
```

Optional artifacts:

```text
candidate_previews/<candidate_id>_uv_layout.png
candidate_previews/<candidate_id>_checker_front.png
uv_side_by_side.png
checker_front_side_by_side.png
checker_side_side_by_side.png
```

Rules:

- Baseline means first strict user seam unwrap before layout optimization replacement.
- Selected means the final selected candidate after candidate sweep.
- Camera framing must be stable between baseline and selected renders.
- Preview material must not be persisted into the source working model.
- `selected_uv.blend` should contain the selected UV result, not checker-only material changes unless explicitly intended.

---

## 8. Electron MVP 3 UX

MVP 3 first screen is Generate + Optimize workspace.

Required layout:

```text
Top Bar
  Open Project | Validate Seam Spec | Generate UV | Optimize | Next: AI Review | Export later

Left Panel
  Project
  Working Model
  Active Seam Spec
  UV Runs

Center
  Tabs: Before/After Checker | UV Layout | Candidate Table
  Before/After view: baseline vs selected
  UV Layout view: baseline vs selected

Right Panel
  Seam Integrity
  Selected Candidate
  Metrics
  Issues
  Run Options

Bottom Panel
  Job progress
  Logs
  Raw reports
```

Required interactions:

- validate active seam spec
- start generate/optimize run
- cancel running job or recover from timeout
- inspect selected candidate metrics
- compare baseline vs selected
- open raw `p5_gate.json` and `seam_report.json`
- reveal artifacts
- continue to MVP 4/5 only when run status is accepted

Candidate table columns:

```text
selected
candidate id
unwrap method
minimize iters
margin
pack shape
rotate
stretch
worst island
texel variance
raster overlap
packing
score
accepted/reason
```

UI wording:

- Do not say “production-ready” solely from metrics.
- Use “selected candidate” and “no blocking overlap detected”.
- If baseline is kept, say “Baseline retained; candidates did not clear the improvement threshold.”

---

## 9. Project State Contract

`project.json` MVP 3 extension:

```json
{
  "latest_uv_generate_run_id": "uv_run_uuid",
  "uv_generate_runs": ["uv_run_uuid"],
  "selected_uv_model": "work/uv/selected_uv.blend",
  "selected_uv_summary": "work/uv/selected_uv_summary.json",
  "latest_derived_seam_spec": "work/seams/derived_from_uv_boundary.json"
}
```

`latest_derived_seam_spec`은 UV boundary fallback으로 accepted된 run이 파생한 spec을 가리키는 optional pointer다. `active_user_seam_spec`(Seam Editor에서 저장한 spec)을 절대 덮어쓰지 않는다.

`runs/<uv_run_id>/status.json`:

```json
{
  "schema_version": 1,
  "run_id": "uv_run_uuid",
  "command": "generate_uv_from_seams",
  "status": "queued | running | accepted | needs_user_review | needs_input | failed | cancelled",
  "started_at": "2026-06-20T00:00:00.000Z",
  "finished_at": null,
  "input": {
    "model": "../../work/working_lowpoly.blend",
    "object_name": "SM_Test_Pottery_a_02",
    "seam_spec": "../../work/seams/user_seam_spec.json"
  },
  "artifacts": {},
  "error": null
}
```

`work/uv/selected_uv_summary.json` should copy the accepted `uv_generate_summary.json` plus source run id, so MVP 4/5 can read one stable file.

---

## 10. Implementation Strategy

Recommended worker shape:

```text
worker/generate_uv_from_seams.py
  - parse job JSON
  - open/import model
  - select object
  - load and validate UserSeamSpec
  - run chart_uv_agent.pipeline.run_chart_uv with strict flags
  - normalize p5_gate/seam_report/candidate_summary
  - render baseline/selected previews
  - save selected_uv.blend
  - write uv_generate_summary.json and status.json
```

Two acceptable backend approaches:

1. Direct Python call path:
   - Open model inside Blender.
   - Extract `MeshGraph`.
   - Call `run_chart_uv()` directly.
   - Best long-term path for app worker.

2. Wrapper around `worker/run_quad_retopo_job.py` P5:
   - Faster initial integration if P5 resume inputs already match.
   - Must still normalize outputs into MVP 3 contract.
   - Avoid leaking `target_faces`/`P5` terminology into Electron contracts.

Preferred production direction: direct Python call path.

---

## 11. 병렬 작업 분해

다른 세션에 나눠 맡길 때 아래 단위로 분리한다. 한 세션이 여러 영역의 파일을 동시에 소유하지 않게 한다.

### Session A: App UV Generate Worker Contract

Owner files:

```text
worker/generate_uv_from_seams.py
worker/app_uv_generate_contract.py
tests/test_uv_generate_contract.py
```

Tasks:

- `generate_uv_from_seams` job schema 구현
- status lifecycle 작성
- model open/import 지원
- UserSeamSpec validation 연결
- strict user/reference default options 적용
- summary normalization

Acceptance:

- missing seam spec은 structured error가 된다.
- invalid seam spec은 selected UV를 저장하지 않는다.
- accepted run은 `uv_generate_summary.json`을 남긴다.

### Session B: Pipeline Integration + Seam Integrity

Owner files:

```text
worker/generate_uv_from_seams.py
chart_uv_agent/pipeline.py
tests/test_uv_generate_seam_integrity.py
```

Tasks:

- direct `run_chart_uv()` integration
- strict flags가 실제로 적용되는지 검증
- `auto_added_seams == 0` assertion/report
- `final_seam_count == user_seam_count` assertion/report
- protected edge leakage 검증
- mandatory diagnostics report-only 유지

Acceptance:

- MVP 2 reference-boundary spec으로 seam count가 바뀌지 않는다.
- auto refine/repair/mandatory flags가 기본 false다.
- seam integrity 실패 시 `needs_user_review` 또는 `failed`가 된다.

### Session C: Candidate Summary + Layout Optimization Reports

Owner files:

```text
chart_uv_agent/layout_optimization.py
worker/generate_uv_from_seams.py
tests/test_uv_generate_candidates.py
```

Tasks:

- `layout_optimization.report()`를 app summary로 normalize
- `candidate_summary.json` 작성
- rejected candidates list 생성
- selected candidate metrics flatten
- baseline retained case 처리

Acceptance:

- candidate list가 UI-friendly schema로 저장된다.
- selected candidate id가 summary/candidate_summary/p5_gate에서 일치한다.
- candidate_count가 `layout_opt_max_candidates` 이하로 제한된다.

### Session D: Preview and Selected UV Artifacts

Owner files:

```text
uv_agent/blender/review_render.py
worker/generate_uv_from_seams.py
tests/test_uv_generate_artifacts.py
```

Tasks:

- baseline UV layout/checker render
- selected UV layout/checker render
- stable camera framing
- `selected_uv.blend` 저장
- `work/uv/selected_uv_summary.json` copy

Acceptance:

- required six image artifacts가 생성된다.
- selected `.blend`가 다시 열리고 UV layer를 포함한다.
- source `working_model`을 덮어쓰지 않는다.

### Session E: Electron Main UV Generate Service

Owner files:

```text
app/electron/main/uvGenerate*
app/shared/contracts/uvGenerate*
app/electron/main/project*
```

Tasks:

- IPC handlers:
  - `uvGenerate:validateInput`
  - `uvGenerate:start`
  - `uvGenerate:cancel`
  - `uvGenerate:getRun`
  - `uvGenerate:getCandidateSummary`
- worker spawn/cancel
- project path resolve
- project.json MVP 3 fields update
- artifact path normalization

Acceptance:

- mock worker로 accepted run이 project state에 기록된다.
- failed run이 UI에 structured error로 전달된다.
- latest run 재열기가 가능하다.

### Session F: Renderer Generate + Optimize UI

Owner files:

```text
app/electron/renderer/uv-generate/*
app/shared/contracts/uvGenerate*
```

Tasks:

- Generate + Optimize workspace 구현
- seam spec validation panel
- run options panel
- job progress/log panel
- before/after checker view
- UV layout comparison view
- candidate table
- raw report tabs

Acceptance:

- 사용자가 validate -> generate -> compare -> selected candidate inspect를 완료할 수 있다.
- baseline retained / failed / needs_user_review 상태가 각각 명확히 보인다.
- candidate table이 작은 화면에서도 깨지지 않는다.

### Session G: QA Fixtures + E2E Smoke

Owner files:

```text
tests/e2e/test_mvp3_uv_generate.py
sample/
docs/MVP3_QA_RESULTS.ko.md
```

Tasks:

- simple seam spec fixture
- no/invalid seam spec fixture
- sample pottery reference-boundary smoke
- Blender 없는 환경 skip 정책
- Electron main/renderer smoke
- artifact and metric checklist 기록

Acceptance:

- Blender가 없으면 tests skip으로 통과한다.
- Blender가 있으면 generate/optimize smoke가 돈다.
- QA 결과 문서에 command, artifact path, candidate count, selected id, seam integrity 결과가 남는다.

---

## 12. 구현 순서

권장 순서:

1. `app_uv_generate_contract.py` 작성
2. `generate_uv_from_seams` worker skeleton과 status lifecycle 구현
3. UserSeamSpec validation 연결
4. strict user/reference flags 적용
5. `run_chart_uv()` direct integration
6. `uv_generate_summary.json` normalization
7. `candidate_summary.json` normalization
8. baseline/selected preview artifacts 생성
9. `selected_uv.blend`와 `work/uv/selected_uv_summary.json` 저장
10. Electron main IPC/service 구현
11. Renderer workspace 구현
12. QA smoke와 결과 문서 작성

세션 병렬화 기준:

- Session A가 summary contract를 먼저 고정한다.
- Session F는 mock `uv_generate_summary.json`과 `candidate_summary.json`으로 선개발 가능하다.
- Session E는 mock worker로 선개발 가능하다.
- Session D는 MVP 1 review render helper를 재사용해 독립 개발 가능하다.
- Session G는 invalid spec tests를 먼저 만들고 Blender smoke를 후반에 붙인다.

---

## 13. Production Acceptance Checklist

Functional:

- [ ] MVP 2 project에서 `active_user_seam_spec`을 찾을 수 있다.
- [ ] seam spec validation을 실행할 수 있다.
- [ ] Generate UV run을 시작할 수 있다.
- [ ] strict user/reference defaults가 적용된다.
- [ ] baseline UV가 생성된다.
- [ ] layout optimization candidate list가 생성된다.
- [ ] selected candidate id가 표시된다.
- [ ] before/after UV layout을 볼 수 있다.
- [ ] before/after checker preview를 볼 수 있다.
- [ ] selected UV model이 `work/uv/selected_uv.blend`에 저장된다.
- [ ] project.json에 latest UV generate run과 selected UV paths가 기록된다.

Seam integrity:

- [ ] `auto_added_seams == 0`
- [ ] `final_seam_count == user_seam_count`
- [ ] protected edge가 자동으로 seam으로 바뀌지 않는다.
- [ ] mandatory 90 diagnostics가 gate를 fail시키지 않는다.
- [ ] invalid edge ids가 있으면 run이 selected output을 만들지 않는다.

Layout quality:

- [ ] selected candidate는 raster overlap hard reject를 통과한다.
- [ ] selected candidate는 UV bounds check를 통과한다.
- [ ] selected candidate는 baseline regression guard를 통과하거나 baseline retained로 기록된다.
- [ ] candidate_count가 옵션 cap 이하이다.

Robustness:

- [ ] Blender executable path가 없으면 setup error를 보여준다.
- [ ] worker failure가 app crash로 이어지지 않는다.
- [ ] stdout/stderr log가 run folder에 저장된다.
- [ ] image artifact 실패는 warning으로 표시된다.
- [ ] source working model을 덮어쓰지 않는다.

Quality:

- [ ] Python tests 통과
- [ ] Electron typecheck 통과
- [ ] Electron renderer build 통과
- [ ] sample pottery generate/optimize smoke 결과 문서화

---

## 14. 위험 요소와 대응

### Candidate sweep이 source object UV를 계속 덮어씀

기존 layout optimization은 candidate 측정 중 object UV를 덮어쓴다.

대응:

- selected spec을 마지막에 반드시 재적용한다.
- candidate별 full UV state 저장은 MVP 3.1로 미룬다.
- manual candidate reselection은 selected spec 재적용 방식으로 구현한다.

### Seam set이 의도치 않게 바뀔 위험

repair/mandatory/auto refine 옵션이 켜지면 user seam source of truth가 깨진다.

대응:

- app worker default를 strict false flags로 고정한다.
- summary에 actual flags를 기록한다.
- seam integrity assertion을 hard acceptance로 둔다.

### `run_quad_retopo_job.py` phase coupling

기존 worker는 retopo/P5/P6 흐름에 묶여 있다.

대응:

- Electron contract는 `generate_uv_from_seams`로 분리한다.
- 내부 재사용은 허용하지만 P5 terminology를 UI/API로 노출하지 않는다.
- 장기적으로 direct `run_chart_uv()` worker로 수렴한다.

### “개선”의 정의가 모호함

Packing이 좋아졌지만 stretch가 나빠질 수 있다.

대응:

- `layout_optimization.py`의 score + no-regression guard를 사용한다.
- UI는 score와 주요 metrics before/after를 모두 보여준다.
- baseline retained case를 정상 결과로 취급한다.

### Heavy preview cost

candidate별 preview를 모두 렌더링하면 느리다.

대응:

- MVP 3 필수 preview는 baseline과 selected만이다.
- candidate별 preview는 optional/lazy로 둔다.
- candidate table은 metrics 중심으로 먼저 표시한다.

---

## 15. Handoff Notes for Other Sessions

작업 시작 전 확인:

```bash
git status --short
rg "run_chart_uv|layout_optimization|user_seam_spec|auto_refine_user_seams|p5_gate" chart_uv_agent artist_uv_agent worker tests docs
```

다른 세션 규칙:

- 각 세션은 자신의 owner files만 수정한다.
- shared contract 변경은 먼저 이 문서의 Generate UV Contract, Candidate Summary Contract, Project State Contract를 갱신한다.
- MVP 3 worker는 user seam spec을 overwrite하지 않는다.
- MVP 3 worker는 source `working_model`을 overwrite하지 않는다.
- generated UV output, previews, smoke outputs는 project folder 또는 `.context/`에 저장한다.
- 큰 sample asset은 git에 추가하지 말고 `.context/attachments` 또는 별도 download instruction으로 둔다.

권장 PR 설명:

```text
MVP3 adds user-seam UV generation and layout optimization:
- validates the MVP2 active user seam spec
- runs strict user/reference seam unwrap with no auto seam changes
- evaluates layout optimization candidates and selects the best safe candidate
- writes normalized summaries, candidate reports, before/after previews, and selected UV blend
- exposes Electron IPC and UI for Generate + Optimize review
```

---

## 16. MVP 3 Done Definition

MVP 3는 다음 demo가 한 번에 성공하면 완료로 본다.

```text
1. App opens an MVP 2 project.
2. App finds project.json -> working_model + active_user_seam_spec.
3. User opens Generate + Optimize workspace.
4. App validates the seam spec.
5. User starts Generate UV.
6. Worker runs strict user/reference seam mode.
7. Worker produces baseline UV.
8. Worker evaluates layout optimization candidates.
9. Worker selects a candidate or explicitly keeps baseline.
10. App shows candidate table and selected candidate.
11. App shows baseline vs selected UV layout.
12. App shows baseline vs selected checker front/side.
13. seam integrity reports auto_added_seams == 0 and final_seam_count == user_seam_count.
14. project.json points to selected_uv_model and latest_uv_generate_run_id.
15. selected_uv.blend can be opened by the next MVP worker.
```

이 상태가 되면 MVP 4 AI Review 세션은 `p5_gate.json`, `seam_report.json`, `candidate_summary.json`, UV layout screenshots, checker renders, and `user_seam_spec.json`을 읽고 report explanation/suggestion workflow를 시작할 수 있다.
