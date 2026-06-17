# Electron Retopo + UV Review App PRD

> 대상: 다음 구현/기획 세션, Electron app 작업자, Blender/Python pipeline 작업자  
> 제품 방향: **high-poly를 low-poly로 만들고, 3D modeler가 seam/chapter를 직접 결정한 뒤, 앱이 UV review, unwrap, relax, pack, 검증, 비교, export를 쉽게 해주는 도구**  
> 핵심 원칙: AI가 몰래 seam을 확정하지 않는다. 사용자/reference seam을 source of truth로 둔다.

---

## 1. 배경

현재 프로젝트의 원래 목표는 다음과 같았다.

```text
high polygon object
  -> low polygon model 생성
  -> low polygon 기반 UV 생성
```

지금까지의 결론:

1. Low-poly 생성은 1차로 만족 가능한 수준이다.
2. 문제는 UV seam/chapter 자동 생성 품질이었다.
3. 완전 자동 seam/chapter 생성으로 artist-quality UV를 보장하는 것은 현재 단계에서 적절하지 않다.
4. 따라서 앱은 **high -> low preparation은 자동화/반자동화하고, seam/chapter 결정은 3D modeler에게 맡기며, 그 이후 UV unwrap/relax/pack/review/export를 자동화**하는 방향이 현실적이다.

모델러가 실제로 귀찮아하는 부분:

- high-poly를 적절한 target face count의 low-poly로 만드는 반복
- silhouette/shape가 얼마나 유지됐는지 확인
- low-poly 결과를 다시 DCC로 가져가 검토
- 기존 UV가 있는지 확인
- checker를 입혀 distortion 확인
- seam을 조금 바꾸고 unwrap 반복
- texel density 맞추기
- island rotate/scale/pack 반복
- overlap/stretched area 찾기
- 여러 후보 중 어떤 UV가 나은지 비교
- low-poly + UV 결과를 FBX/OBJ/GLB로 export

이 앱은 Blender 대체 DCC가 아니라, **high-poly -> low-poly -> user-guided UV -> export를 한 프로젝트 안에서 반복 검토하는 도구**다.

---

## 2. 핵심 제품 결정

### 2.1 Seam/chapter 결정은 사용자에게 맡긴다

자동 seam/chapter 생성은 메인 기능이 아니다.

기본 제품 흐름:

```text
high-poly import
  -> low-poly 생성 또는 기존 low-poly import
  -> low-poly review
  -> 사용자/import/reference
  -> seam/chapter 지정 또는 기존 UV boundary 추출
  -> app이 unwrap/relax/pack/검증/비교
  -> 사용자 승인
  -> export
```

### 2.2 AI/Nemotron은 결정자가 아니라 reviewer/recommender다

AI가 직접 edge id를 확정해서 자동으로 seam을 추가하면 위험하다.

AI 역할:

- report 요약
- 문제 원인 설명
- 후보 비교 설명
- “이 근처에 seam 추가를 고려하세요” 수준의 suggestion
- 사용자가 승인하면 rerun

AI가 하면 안 되는 것:

- 사용자 몰래 seam 추가
- 사용자 몰래 protected edge 해제
- mandatory rule을 다시 켜서 reference seam을 바꿈
- 최종 UV를 “좋다”고 단정하고 report를 숨김

### 2.3 User/reference seam mode 기본 규칙

현재 pipeline에서 user/reference seam mode 기본값은 다음이어야 한다.

```text
auto_refine_user_seams = false
repair_user_seams = false
enforce_user_mandatory = false
gate_user_mandatory = false
optimize_layout = true
```

이유:

- 사용자가 준 seam/reference UV island boundary가 source of truth다.
- mandatory 90 rule을 기본으로 켜면 앱이 뒤에서 seam을 추가하거나 gate fail을 만들어 사용자를 혼란스럽게 한다.
- mandatory 90 관련 수치는 report-only diagnostic으로 남길 수 있지만, 기본 gate 실패/자동 보정 원인이 되면 안 된다.

---

## 3. 현재 pipeline 상태

중요 코드:

```text
retopo_agent/
worker/run_retopo_job.py
worker/run_quad_retopo_job.py
chart_uv_agent/pipeline.py
chart_uv_agent/unwrap.py
chart_uv_agent/layout_optimization.py
artist_uv_agent/user_seams.py
artist_uv_agent/seam_report.py
uv_agent/geometry/evaluation.py
```

현재 가능한 것:

- high-poly -> proxy/low-poly/adaptive decimation/retopo 계열 worker 코드
- low-poly shape/silhouette gate 계열 report
- OBJ/FBX/Blend 기반 mesh를 Blender worker에서 처리
- user seam spec JSON 로드
- user seam 기준 unwrap
- mandatory 90 rule/gate 끄기 가능
- layout optimization loop 실행 가능
- checker render 생성
- UV layout PNG 생성
- `p5_gate.json`, `seam_report.json` 생성
- FBX/OBJ/GLB export는 앱 MVP에서 정리 필요

최근 pottery FBX 테스트:

```text
.context/attachments/qMjqaX/SM_Test_Pottery_a_02.fbx
```

mesh:

```text
object: SM_Test_Pottery_a_02
vertices: 6562
edges: 18701
faces: 12152
uv layer: UVChannel_1
```

reference UV boundary seam:

```text
user seam: 1230
auto-added seam: 0
final seam: 1230
```

layout optimization 결과:

```text
selected: slim_concave_m002
packing: 0.583109 -> 0.591278
stretch: 0.06866 -> 0.06866
worst island distortion: ~0.203 유지
raster overlap: 0.0
gate: accepted
```

결론:

- seam을 건드리지 않고도 unwrap/pack 후보 탐색은 작동한다.
- 아직 시각적으로 dramatic한 개선은 아니지만, MVP 후단 최적화 루프는 시작 가능하다.

---

## 4. 제품 목표

### 4.1 Primary goal

3D modeler가 **high-poly -> low-poly -> UV -> export** 작업을 더 빠르게 검토/생성/수정/비교할 수 있게 한다.

### 4.2 What success looks like

사용자는 다음 작업을 앱 안에서 할 수 있어야 한다.

1. high-poly 또는 low-poly 모델을 import한다.
2. high-poly만 있으면 target face count를 정하고 low-poly를 생성한다.
3. low-poly shape/silhouette 결과를 확인한다.
4. 기존 UV가 있으면 즉시 확인한다.
5. checker preview로 stretch를 본다.
6. overlap/density/stretch report를 본다.
7. edge를 선택해 seam/protect를 지정한다.
8. user seam spec JSON을 저장한다.
9. UV generate/optimize를 실행한다.
10. 후보별 before/after를 비교한다.
11. AI review로 문제를 이해한다.
12. 승인 후 FBX/OBJ/GLB로 export한다.

---

## 5. Target User

주 사용자:

- 3D modeler
- game asset artist
- environment/prop artist
- low-poly cleanup artist
- Blender/Maya/3ds Max/ZBrush workflow 사용자

사용자의 기대:

- UV를 완전 자동으로 마법처럼 만들기보다, 본인이 seam 의도를 주면 나머지 반복 작업을 줄여주길 원함.
- checker가 늘어나는지 빠르게 보고 싶음.
- texel density가 맞는지 보고 싶음.
- UV island가 겹치는지 바로 알고 싶음.
- export가 안정적이어야 함.

---

## 6. App Architecture

### 6.1 High-level

```text
Electron App
  React UI
  Three.js 3D Viewer
  UV Editor Canvas
  Job Queue / Project History
  AI Review Panel

Local Worker
  Blender background process
  Retopo / decimation pipeline
  Python UV pipeline
  Report generator
  Exporter

Project Folder
  highpoly source model
  source model
  lowpoly result
  retopo reports
  seam spec JSON
  UV candidates
  reports
  exports
```

### 6.2 Electron main process

Responsibilities:

- local project folder 관리
- file import/export dialog
- worker process spawn
- job queue 관리
- IPC bridge
- local settings
- Nemotron API credential 관리

### 6.3 Renderer process

Recommended stack:

```text
React
TypeScript
Three.js / react-three-fiber
Canvas/SVG UV editor
TanStack Query or simple job state store
Zustand or Redux lightweight store
```

Renderer views:

- Project dashboard
- 3D viewport
- UV editor
- Seam/chapter editor
- Candidate comparison
- Report panel
- AI review panel
- Export panel

### 6.4 Worker

Current backend can stay Python/Blender-first.

Worker commands should eventually expose:

```text
inspect_model
generate_lowpoly
inspect_lowpoly_result
extract_existing_uv
extract_uv_boundary_as_seams
save_user_seam_spec
generate_uv
optimize_layout
render_checker_preview
export_model
```

Worker output should always be structured JSON plus image artifacts.

---

## 7. Data Model

### 7.1 Project

```json
{
  "id": "project_uuid",
  "name": "pottery_test",
  "source_model": "source/highpoly_or_lowpoly.fbx",
  "source_model_role": "highpoly | lowpoly | unknown",
  "highpoly_model": "source/highpoly.fbx",
  "lowpoly_model": "work/lowpoly.blend",
  "working_model": "work/lowpoly.blend",
  "created_at": "...",
  "updated_at": "..."
}
```

### 7.2 Mesh Summary

```json
{
  "object_name": "SM_Test_Pottery_a_02",
  "vertices": 6562,
  "edges": 18701,
  "faces": 12152,
  "uv_layers": ["UVChannel_1"],
  "materials": ["default"],
  "mesh_role": "highpoly | lowpoly | unknown",
  "recommended_next_step": "generate_lowpoly | review_uv | inspect"
}
```

### 7.3 Low-poly Result

```json
{
  "run_id": "uuid",
  "input_highpoly": "source/highpoly.fbx",
  "lowpoly_model": "work/lowpoly.blend",
  "target_faces": 12000,
  "actual_faces": 12152,
  "status": "accepted",
  "metrics": {
    "shape_distance_mean": 0.0,
    "shape_distance_max": 0.0,
    "normal_deviation_mean": 0.0,
    "non_manifold_edges": 0
  },
  "artifacts": {
    "retopo_report": "reports/retopo_report.json",
    "preview_front": "previews/lowpoly_front.png",
    "preview_overlay": "previews/high_low_overlay.png"
  }
}
```

### 7.4 User Seam Spec

Current format:

```json
{
  "version": 1,
  "object": "SM_Test_Pottery_a_02",
  "mode": "user_seams",
  "mandatory_fold_angle": 90.0,
  "user_seam_edges": [16, 113, 138],
  "user_protected_edges": [],
  "chapters": [],
  "notes": "Extracted from UV island boundaries"
}
```

Future extension:

```json
{
  "chapters": [
    {
      "name": "body",
      "face_ids": [1, 2, 3],
      "seam_edges": [10, 11],
      "protected_edges": [20, 21]
    }
  ]
}
```

### 7.5 UV Candidate

```json
{
  "id": "slim_concave_m002",
  "unwrap_method": "MINIMUM_STRETCH",
  "minimize_iters": 0,
  "margin": 0.002,
  "pack_shape": "CONCAVE",
  "rotate": true,
  "average_scale": true,
  "metrics": {
    "stretch_score": 0.06866,
    "worst_island_distortion": 0.202999,
    "raster_overlap_ratio": 0.0,
    "texel_density_variance": 0.000002,
    "packing_efficiency": 0.591278
  },
  "score": -0.003276,
  "selected": true
}
```

### 7.6 Reports

Required files per project run:

```text
retopo_report.json
p5_gate.json
seam_report.json
uv_layout.png
checker_front.png
checker_side.png
candidate_summary.json
```

---

## 8. MVP Roadmap

---

# MVP 0: High to Low Preparation

## Goal

high-poly 모델을 import하고, target face count에 맞는 low-poly 결과를 생성/검토한다. 사용자가 이미 low-poly를 갖고 있으면 이 단계는 skip할 수 있다.

## Features

### 1. High-poly import

지원 형식:

- FBX
- OBJ
- GLB/GLTF
- Blend optional

### 2. Target setup

사용자가 설정할 수 있어야 한다.

```text
target face count
preserve silhouette strength
preserve sharp edges / material boundaries / existing UV seams
cleanup level
```

### 3. Low-poly generation

기존 worker/pipeline을 사용한다.

관련 코드:

```text
retopo_agent/
worker/run_retopo_job.py
worker/run_quad_retopo_job.py
```

가능한 초기 모드:

- adaptive decimation
- quad/tri cleanup
- shape/silhouette gate
- retry ladder

### 4. Low-poly review

UI에서 보여줄 것:

- high vs low overlay
- face count
- vertex count
- component count
- non-manifold warning
- silhouette/shape distance summary
- normal deviation summary

### 5. Low-poly handoff

low-poly 결과는 이후 UV 단계의 working mesh가 된다.

```text
highpoly source
  -> lowpoly working model
  -> UV review/seam/unwrap/pack
```

## Acceptance Criteria

- high-poly 또는 existing low-poly를 import할 수 있다.
- high-poly 입력에서 low-poly 결과를 생성할 수 있다.
- target face count와 실제 face count를 UI에 표시한다.
- shape/silhouette report를 표시한다.
- 사용자가 low-poly 결과를 승인하면 MVP 1 UV Review로 넘어간다.

---

# MVP 1: UV Review App

## Goal

low-poly working model을 기준으로 기존 UV를 보여주며, checker preview와 stretch/overlap/density report를 제공한다.

## Features

### 1. Low-poly model load

지원 형식:

- FBX
- OBJ
- GLB/GLTF

초기 MVP에서는 MVP 0에서 생성한 low-poly working model 또는 사용자가 직접 import한 low-poly를 Blender worker로 읽고 mesh summary를 반환한다.

Required output:

```json
{
  "object_count": 1,
  "objects": [
    {
      "name": "SM_Test_Pottery_a_02",
      "vertices": 6562,
      "edges": 18701,
      "faces": 12152,
      "uv_layers": ["UVChannel_1"]
    }
  ]
}
```

### 2. Existing UV viewer

기존 UV layer가 있으면:

- UV layout PNG 생성
- UV island count 표시
- UV bounds 표시
- overlap 여부 표시

### 3. Checker preview

3D viewport에서 checker material 적용.

필수 view:

- front
- side
- free orbit

### 4. Report

표시할 metric:

- stretch score
- worst island distortion
- raster overlap
- signed overlap
- texel density variance
- packing efficiency
- island count
- UV bounds

## Acceptance Criteria

- FBX/OBJ/GLB 하나를 import할 수 있다.
- 기존 UV가 있으면 UV layout을 볼 수 있다.
- checker preview가 보인다.
- report JSON이 UI에 표시된다.
- mandatory 90 rule은 기본 report/gate에 끼지 않는다.

---

# MVP 2: User Seam Spec Editor

## Goal

사용자가 edge를 선택하고, Mark Seam / Protect를 지정한 뒤 seam spec JSON을 저장한다.

## Features

### 1. Edge selection

3D viewport에서 edge 선택.

초기 구현 선택지:

1. Three.js에서 raycast로 edge 선택
2. Blender worker에 selection query 전달
3. 단기적으로는 Blender add-on/bridge 사용도 가능

MVP에서는 정확도가 중요하므로 Blender mesh edge id와 UI edge id가 일치해야 한다.

### 2. Mark Seam / Protect

선택한 edge에 다음 상태 부여:

```text
normal
seam
protected
```

### 3. Seam spec JSON 저장

저장 형식:

```json
{
  "version": 1,
  "object": "ObjectName",
  "mode": "user_seams",
  "user_seam_edges": [],
  "user_protected_edges": [],
  "chapters": []
}
```

### 4. Existing UV boundary extraction

기존 UV가 있는 asset은 UV island boundary를 seam spec으로 변환할 수 있어야 한다.

이 기능은 매우 중요하다.

사용 예:

```text
Reference UV exists
  -> Extract UV boundary
  -> Use as user_seam_edges
  -> Re-unwrap/optimize/pack
```

## Acceptance Criteria

- edge 선택이 가능하다.
- seam/protect 상태가 UI에 표시된다.
- seam spec JSON 저장/로드가 가능하다.
- 기존 UV boundary를 seam spec으로 추출할 수 있다.
- 저장한 spec으로 worker가 UV generate를 실행할 수 있다.

---

# MVP 3: Generate + Optimize

## Goal

현재 Python/Blender pipeline과 연결해 user seam 기반 UV를 generate하고, unwrap/relax/pack 후보를 비교해 best candidate를 선택한다.

## Features

### 1. Generate UV

기본 실행 옵션:

```bash
--uv-engine chart
--user-seam-spec <path>
--auto-refine-user-seams false
--repair-user-seams false
--enforce-user-mandatory false
--gate-user-mandatory false
```

### 2. Optimize layout

기본 실행 옵션:

```bash
--optimize-layout true
--layout-opt-preset user_reference
--layout-opt-max-candidates 24
```

후보 축:

- unwrap method
  - `MINIMUM_STRETCH`
  - `ANGLE_BASED`
- relax/minimize iterations
- average island scale
- pack margin
- pack shape
  - `CONCAVE`
  - `AABB`
- rotate

### 3. Best candidate 선택

score 기준:

- checker stretch 낮음
- worst island distortion 낮음
- texel density variance 낮음
- overlap 없음
- packing efficiency 높음

### 4. Before/after 비교

UI에서 다음을 비교:

```text
baseline UV
optimized UV
candidate list
checker preview before/after
metric table
```

## Acceptance Criteria

- seam set이 변경되지 않는다.
- `auto_added_seams == 0`
- `final_seam_count == user_seam_count`
- candidate list가 생성된다.
- selected candidate가 표시된다.
- optimized layout이 baseline보다 같거나 개선된다.
- overlap이 없어야 한다.

---

# MVP 4: AI Review

## Goal

Nemotron이 UV report를 읽고 자연어로 설명하고, 수정 제안을 한다. 사용자가 승인하면 rerun한다.

## AI Inputs

```text
p5_gate.json
seam_report.json
layout_optimization candidates
UV layout screenshot
checker renders
user seam spec
```

## AI Outputs

예시:

```text
이 UV는 export 가능하지만 base strip 쪽 texel density가 dome보다 약간 큽니다.
현재 best candidate는 packing을 0.583 -> 0.591로 개선했고 stretch는 유지했습니다.
다음 개선으로 base underside island를 별도 seam으로 분리하면 checker가 더 균일해질 수 있습니다.
```

AI가 제안할 수 있는 action:

- 특정 area inspect
- selected island relax
- pack margin 변경
- 후보 수 늘리기
- user에게 seam 추가 요청
- rerun optimization

AI가 직접 수행하면 안 되는 action:

- 사용자 승인 없는 seam 추가
- user seam spec overwrite
- mandatory repair 강제 켜기
- 실패 report 숨기기

## Acceptance Criteria

- AI가 report를 요약한다.
- 문제 metric을 설명한다.
- 수정 제안을 한다.
- 사용자가 approve/reject할 수 있다.
- approve 시 worker job rerun이 가능하다.

---

# MVP 5: Production Export

## Goal

최종 UV 결과를 production asset으로 export하고, project history와 rollback을 제공한다.

## Features

### 1. Export

지원 형식:

- FBX
- OBJ
- GLB/GLTF

Export options:

- selected UV layer
- apply scale
- include materials
- include normals
- copy textures

### 2. Project history

각 run 저장:

```json
{
  "run_id": "uuid",
  "created_at": "...",
  "input_model": "...",
  "seam_spec": "...",
  "selected_candidate": "...",
  "metrics": "...",
  "artifacts": {
    "blend": "...",
    "obj": "...",
    "uv_layout": "...",
    "checker_front": "..."
  }
}
```

### 3. Candidate rollback

사용자는 이전 후보로 되돌릴 수 있어야 한다.

Rollback 대상:

- seam spec
- UV candidate
- export result

## Acceptance Criteria

- FBX/OBJ/GLB export가 가능하다.
- export 결과가 DCC에서 다시 열린다.
- project history가 남는다.
- 이전 candidate로 rollback 가능하다.
- report와 exported asset이 연결된다.

---

## 9. UX Layout Proposal

### 9.1 Main workspace

```text
┌─────────────────────────────────────────────────────────────┐
│ Top Bar: Import | Generate UV | Optimize | AI Review | Export │
├───────────────┬───────────────────────────┬─────────────────┤
│ Project Tree  │ 3D View                   │ UV View          │
│ Object List   │ checker / seam / heatmap  │ islands / pack   │
│ UV Layers     │                           │ overlap overlay  │
│ Chapters      │                           │                 │
├───────────────┴───────────────────────────┴─────────────────┤
│ Candidate Timeline / Metrics Table / AI Review               │
└─────────────────────────────────────────────────────────────┘
```

### 9.2 3D View overlays

- checker
- seams
- protected edges
- selected edges
- stretch heatmap
- overlap highlight
- texel density heatmap

### 9.3 UV View overlays

- island wireframe
- selected island
- overlap pixels
- density color
- candidate before/after ghost

### 9.4 Candidate Table

Columns:

```text
selected
candidate id
unwrap method
relax iterations
margin
pack shape
stretch
worst island
texel variance
overlap
packing
score
```

---

## 10. Worker API Draft

Electron main process can call worker commands through JSON.

### 10.1 `inspect_model`

Input:

```json
{
  "command": "inspect_model",
  "path": "source/model.fbx"
}
```

Output:

```json
{
  "objects": [
    {
      "name": "SM_Test_Pottery_a_02",
      "vertices": 6562,
      "edges": 18701,
      "faces": 12152,
      "uv_layers": ["UVChannel_1"]
    }
  ]
}
```

### 10.2 `generate_lowpoly`

Input:

```json
{
  "command": "generate_lowpoly",
  "input": "source/highpoly.fbx",
  "target_faces": 12000,
  "options": {
    "preserve_silhouette": true,
    "preserve_sharp_edges": true,
    "cleanup": true
  }
}
```

Output:

```json
{
  "run_id": "uuid",
  "status": "accepted",
  "artifacts": {
    "lowpoly_blend": "work/lowpoly.blend",
    "lowpoly_obj": "work/lowpoly.obj",
    "retopo_report": "reports/retopo_report.json",
    "preview_front": "previews/lowpoly_front.png"
  },
  "metrics": {
    "target_faces": 12000,
    "actual_faces": 12152,
    "vertices": 6562,
    "non_manifold_edges": 0,
    "shape_distance_mean": 0.0,
    "normal_deviation_mean": 0.0
  }
}
```

Notes:

- 초기 Electron MVP는 이미 low-poly가 있는 경우 이 command를 skip할 수 있다.
- 그러나 제품 전체에서 high -> low 단계는 core workflow다.

### 10.3 `extract_uv_boundary`

Input:

```json
{
  "command": "extract_uv_boundary",
  "model": "work/model.blend",
  "object": "SM_Test_Pottery_a_02",
  "uv_layer": "UVChannel_1"
}
```

Output:

```json
{
  "seam_spec_path": "work/seams/reference_boundary.json",
  "user_seam_count": 1230
}
```

### 10.4 `generate_uv`

Input:

```json
{
  "command": "generate_uv",
  "model": "work/model.blend",
  "reference": "work/reference.obj",
  "seam_spec": "work/seams/user.json",
  "options": {
    "auto_refine_user_seams": false,
    "repair_user_seams": false,
    "enforce_user_mandatory": false,
    "gate_user_mandatory": false,
    "optimize_layout": true,
    "layout_opt_max_candidates": 24
  }
}
```

Output:

```json
{
  "run_id": "uuid",
  "gate": "accepted",
  "selected_candidate_id": "slim_concave_m002",
  "artifacts": {
    "blend": "...",
    "uv_layout": "...",
    "checker_front": "...",
    "checker_side": "...",
    "p5_gate": "...",
    "seam_report": "..."
  }
}
```

---

## 11. Metrics

### 11.1 Must show

- `stretch_score`
- `worst_island_distortion`
- `raster_overlap_ratio`
- `overlap_ratio`
- `texel_density_variance`
- `packing_efficiency`
- `island_count`
- `uv_bounds_ok`
- `fallback_used`

### 11.2 Diagnostic only

In user/reference seam mode:

- `mandatory_90_missing`
- `mandatory_90_uv_unsplit`

These must not be shown as default hard failures.

### 11.3 Candidate score

Current score:

```text
4.0 * stretch_score
3.0 * worst_island_distortion
2.0 * texel_density_variance
2.0 * raster_overlap_ratio
1.0 * overlap_ratio
-1.5 * packing_efficiency
0.2 * small_island_ratio
```

Lower is better.

---

## 12. Non-goals

Do not build in early MVP:

- full DCC replacement
- automatic artist-quality seam generation
- fully automatic chapter generation
- fully automatic production-quality retopology for every arbitrary scan
- hidden mandatory seam repair
- Smart UV fallback as shipped result
- neural UV coordinate generation
- direct texture painting
- UDIM workflow
- multi-user cloud collaboration

---

## 13. Risks

### 13.1 Edge ID mismatch

If Electron/Three.js mesh processing changes topology, edge ids may not match Blender worker.

Mitigation:

- Blender worker remains source of truth for mesh topology.
- UI should consume worker-provided indexed geometry.

### 13.2 User expects full automation

The app must communicate that seam/chapter is user-controlled.

Mitigation:

- Use labels like “Suggestion”, “Draft”, “Needs approval”.
- Do not auto-apply AI seam suggestions.

### 13.3 Packing improvement may be small

Current optimization loop improved pottery packing only from `0.583` to `0.591`.

Mitigation:

- Show honest before/after.
- Add more packer candidates later.
- Consider custom island packer after MVP.

### 13.4 Blender dependency

Blender background process can be slow or fragile.

Mitigation:

- job queue
- cancellable jobs
- progress logs
- artifact-based recovery

---

## 14. Implementation Order

Recommended sequence:

1. Electron project scaffold
2. Project folder model
3. Import model through Blender worker
4. Mesh summary
5. High -> low generation command wiring
6. Low-poly shape/silhouette report panel
7. Existing UV layout preview
8. Checker render preview
9. UV report panel
10. Existing UV boundary extraction
11. Seam spec save/load
12. Generate UV with current pipeline
13. Optimize layout candidate table
14. Before/after comparison
15. AI review panel
16. Export
17. History/rollback

---

## 15. First MVP Slice

The smallest useful product slice:

```text
Import FBX
  -> show mesh summary
  -> if high-poly: generate low-poly
  -> show low-poly report
  -> extract existing UV boundary
  -> run Generate + Optimize
  -> show UV layout + checker + metrics
  -> export OBJ
```

This can ship before manual edge editing.

Why:

- It validates the high -> low -> UV product story.
- Many assets already have some UV/reference boundary.
- It validates app shell, worker, report, preview, optimization, export.
- Manual seam editor is harder and can come next.

---

## 16. Definition of Done for PRD MVP 0-3

MVP 0-3 is done when:

- User can import the pottery FBX.
- User can either accept it as low-poly or run high -> low generation on a high-poly asset.
- App shows low-poly face/vertex count and retopo/shape report.
- App shows existing UV layout.
- App shows checker preview.
- App extracts UV boundary as seam spec.
- App runs current user-seam pipeline with layout optimization.
- App shows candidate table.
- App shows before/after metrics.
- App proves:

```text
final_seam_count == user_seam_count
auto_added_seams == 0
raster_overlap_ratio == 0 or under threshold
uv_bounds_ok == true
```

- App exports a usable model.

---

## 17. Notes for Future Sessions

Do not restart the seam automation debate unless explicitly requested.

The current product direction is:

```text
App helps produce/review low-poly from high-poly.
User controls seam/chapter.
App handles UV unwrap, relax, scale, rotate, pack, validate, compare, export.
AI explains and suggests.
```

If another agent works on this, it should start from:

```text
docs/USER_GUIDED_SEAM_UV_PIPELINE_PLAN.ko.md
docs/UV_LAYOUT_OPTIMIZATION_LOOP_PLAN.ko.md
docs/ELECTRON_UV_REVIEW_APP_PRD.ko.md
docs/ADAPTIVE_LOWPOLY_PLAN.md
docs/RETOPO.md
```

and current code paths:

```text
chart_uv_agent/layout_optimization.py
chart_uv_agent/pipeline.py
worker/run_quad_retopo_job.py
artist_uv_agent/user_seams.py
```
