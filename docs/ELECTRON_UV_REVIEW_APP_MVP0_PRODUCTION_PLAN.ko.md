# Electron UV Review App MVP 0 Production Plan

> 기준 PRD: `docs/ELECTRON_UV_REVIEW_APP_PRD.ko.md`  
> 범위: MVP 0 High to Low Preparation을 production-ready 단계로 끌어올리기 위한 구현 계획  
> 대상: 다른 Conductor 세션, Electron app 작업자, Python/Blender worker 작업자, QA/검증 작업자  
> 핵심 목표: high-poly 또는 existing low-poly import부터 low-poly 생성/검토/승인까지를 안정적인 local production workflow로 만든다.

---

## 1. MVP 0 정의

MVP 0은 UV 편집기가 아니다. UV generation, seam editor, AI review, export production workflow는 후속 MVP로 넘긴다.

MVP 0의 제품 완료 상태는 다음이다.

```text
source model import
  -> model inspection
  -> high-poly / low-poly role decision
  -> high-poly면 target face count 기반 low-poly 생성
  -> low-poly quality report와 preview 확인
  -> 사용자가 working low-poly 승인
  -> 프로젝트에 다음 MVP가 사용할 working mesh/artifact 저장
```

MVP 0에서 반드시 보장할 것:

- 사용자가 FBX/OBJ/GLB/GLTF 모델을 가져올 수 있다.
- 모델의 mesh summary를 UI에서 확인할 수 있다.
- high-poly 입력은 low-poly 후보를 생성할 수 있다.
- existing low-poly 입력은 generation을 건너뛰고 바로 승인할 수 있다.
- target face count와 actual face count를 명확히 표시한다.
- topology/shape/silhouette 계열 report를 JSON artifact로 저장하고 UI에 노출한다.
- 승인된 low-poly는 이후 MVP 1+가 읽을 수 있는 project manifest에 기록된다.

MVP 0에서 하지 않을 것:

- seam/chapter 편집
- UV unwrap/pack/optimization
- AI/Nemotron review
- final FBX/OBJ/GLB production export
- cloud sync 또는 multi-user collaboration
- Blender GUI embedding

---

## 2. 현재 코드 기반에서의 출발점

이미 존재하는 backend 자산:

```text
worker/run_retopo_job.py
worker/run_quad_retopo_job.py
retopo_agent/
uv_agent/blender/extract.py
retopo_agent/blender/shape.py
retopo_agent/geometry/validate.py
```

MVP 0에서 우선 사용할 worker:

```text
worker/run_retopo_job.py
```

이 worker는 현재 다음 artifact를 만들 수 있다.

```text
retopo_plan.json
feature_report.json
generation_report.json
quadflow_report.json
validation_report.json
shape_report.json
lowpoly.blend
lowpoly.fbx
preview.png optional
```

`worker/run_quad_retopo_job.py`는 대형 mesh proxy/quad pipeline 실험 자산으로 남긴다. MVP 0 production 기본 path에는 넣지 않는다. 대형 high-poly 대응이 필요한 별도 세션에서만 실험적으로 확장한다.

---

## 3. Production Architecture

MVP 0은 세 레이어로 나눈다.

```text
Electron Renderer
  - project dashboard
  - import/setup screen
  - low-poly generation progress
  - report/preview review
  - approve working mesh

Electron Main
  - local project folder 생성/관리
  - file copy/import
  - Blender worker process spawn
  - job state/polling/log capture
  - artifact manifest 작성
  - IPC API 제공

Python/Blender Worker
  - inspect_model
  - generate_lowpoly
  - render preview
  - topology/shape report 생성
  - structured JSON 출력
```

중요한 production 원칙:

- Renderer는 Blender를 직접 호출하지 않는다.
- Main process가 모든 filesystem/process 권한을 가진다.
- Worker 입출력은 JSON contract로 고정한다.
- 모든 job은 project-local artifact directory에 append-only로 저장한다.
- 실패도 artifact로 남긴다. UI는 stdout parsing이 아니라 JSON status를 우선 읽는다.

---

## 4. Project Folder Contract

MVP 0 project folder 구조:

```text
<project>/
  project.json
  source/
    original.fbx
  work/
    working_lowpoly.blend
    working_lowpoly.fbx
  runs/
    <run_id>/
      job.json
      status.json
      stdout.log
      stderr.log
      retopo_plan.json
      feature_report.json
      generation_report.json
      quadflow_report.json
      validation_report.json
      shape_report.json
      lowpoly.blend
      lowpoly.fbx
      preview.png
  previews/
    <run_id>_front.png
  reports/
    <run_id>_summary.json
```

`project.json` 최소 schema:

```json
{
  "schema_version": 1,
  "id": "project_uuid",
  "name": "pottery_test",
  "created_at": "2026-06-16T00:00:00.000Z",
  "updated_at": "2026-06-16T00:00:00.000Z",
  "source_model": "source/original.fbx",
  "source_model_role": "highpoly",
  "selected_object": "SM_Test_Pottery_a_02",
  "working_model": "work/working_lowpoly.blend",
  "working_model_fbx": "work/working_lowpoly.fbx",
  "approved_lowpoly_run_id": "run_uuid",
  "runs": ["run_uuid"]
}
```

`runs/<run_id>/status.json` 최소 schema:

```json
{
  "schema_version": 1,
  "run_id": "run_uuid",
  "command": "generate_lowpoly",
  "status": "queued | running | accepted | rejected | failed | cancelled",
  "started_at": "2026-06-16T00:00:00.000Z",
  "finished_at": null,
  "input": {
    "source_model": "../../source/original.fbx",
    "object_name": "SM_Test_Pottery_a_02",
    "target_faces": 12000
  },
  "artifacts": {},
  "error": null
}
```

---

## 5. Worker API Contract

Electron Main은 아래 command들을 제공한다. 내부 구현은 한 개의 Node service 또는 여러 script wrapper로 나눠도 되지만, Renderer가 보는 contract는 고정한다.

### 5.1 `inspect_model`

Input:

```json
{
  "command": "inspect_model",
  "project_id": "project_uuid",
  "path": "/absolute/path/to/source.fbx"
}
```

Output:

```json
{
  "schema_version": 1,
  "status": "accepted",
  "objects": [
    {
      "name": "SM_Test_Pottery_a_02",
      "vertices": 6562,
      "edges": 18701,
      "faces": 12152,
      "materials": [],
      "uv_layers": ["UVChannel_1"],
      "bounds": {
        "min": [-1.0, -1.0, -1.0],
        "max": [1.0, 1.0, 1.0]
      },
      "mesh_role_hint": "lowpoly"
    }
  ],
  "recommended_next_step": "approve_existing_lowpoly | generate_lowpoly | inspect_manually"
}
```

Implementation note:

- 새 worker script `worker/inspect_model.py`를 만든다.
- Blender background에서 source file을 import한 뒤 mesh object별 summary를 JSON으로 쓴다.
- 첫 production 버전에서는 role 판단을 heuristic으로 둔다.
- role heuristic 예: faces <= configurable threshold면 `lowpoly`, 매우 크면 `highpoly`, 확신이 없으면 `unknown`.

### 5.2 `generate_lowpoly`

Input:

```json
{
  "command": "generate_lowpoly",
  "project_id": "project_uuid",
  "run_id": "run_uuid",
  "source_model": "/absolute/path/to/project/source/original.fbx",
  "object_name": "SM_Test_Pottery_a_02",
  "target_faces": 12000,
  "options": {
    "mode": "decimation_optimize",
    "preserve_features": true,
    "feature_angle": 30.0,
    "apply_shrinkwrap": true,
    "retry_ladder": true,
    "render_preview": true
  },
  "out_dir": "/absolute/path/to/project/runs/run_uuid"
}
```

Output:

```json
{
  "schema_version": 1,
  "run_id": "run_uuid",
  "status": "accepted",
  "object_name": "SM_Test_Pottery_a_02",
  "result_object_name": "SM_Test_Pottery_a_02_low",
  "metrics": {
    "source_faces": 121520,
    "target_faces": 12000,
    "actual_faces": 12152,
    "target_error_ratio": 0.0127,
    "non_manifold_edges": 0,
    "quad_ratio": 0.0,
    "surface_distance_mean_ratio": 0.0,
    "surface_distance_max_ratio": 0.0,
    "normal_deviation_mean_deg": 0.0
  },
  "artifacts": {
    "lowpoly_blend": "lowpoly.blend",
    "lowpoly_fbx": "lowpoly.fbx",
    "generation_report": "generation_report.json",
    "validation_report": "validation_report.json",
    "shape_report": "shape_report.json",
    "preview": "preview.png"
  },
  "warnings": []
}
```

Implementation note:

- `worker/run_retopo_job.py`를 직접 고치기보다, Electron용 wrapper를 먼저 만든다.
- wrapper 책임은 source import, Blender command 구성, stdout/stderr 저장, 기존 report들을 `summary.json`으로 normalize하는 것이다.
- 기존 worker의 CLI option 이름과 앱 API 이름을 분리한다. 앱 API는 안정적으로 유지하고 worker 내부 option은 바뀔 수 있게 한다.

### 5.3 `approve_lowpoly`

Input:

```json
{
  "command": "approve_lowpoly",
  "project_id": "project_uuid",
  "run_id": "run_uuid"
}
```

Output:

```json
{
  "status": "accepted",
  "working_model": "work/working_lowpoly.blend",
  "working_model_fbx": "work/working_lowpoly.fbx",
  "approved_lowpoly_run_id": "run_uuid"
}
```

Implementation note:

- `runs/<run_id>/lowpoly.blend`를 `work/working_lowpoly.blend`로 copy한다.
- `runs/<run_id>/lowpoly.fbx`가 있으면 `work/working_lowpoly.fbx`로 copy한다.
- `project.json`의 `working_model`, `working_model_fbx`, `approved_lowpoly_run_id`, `updated_at`을 갱신한다.

---

## 6. Electron MVP 0 UX

첫 화면은 landing page가 아니라 실제 project workspace여야 한다.

Required views:

```text
Project Shell
  Top Bar: Import, Inspect, Generate Low-poly, Approve
  Left Panel: Project/source/object/run list
  Center: 3D preview or generated preview image
  Right Panel: Target setup + mesh summary + warnings
  Bottom Panel: job log + report tabs
```

Required interactions:

- 파일 import dialog
- object selection
- source role 선택 또는 confirm
- target face count input
- feature preservation toggle
- generate button
- job progress state
- report tabs:
  - Summary
  - Generation
  - Topology
  - Shape
  - Logs
- approve low-poly button

초기 3D preview 선택:

- production MVP 0에서는 image preview를 우선 허용한다.
- Three.js viewport는 import한 model을 보여줄 수 있으면 좋지만, MVP 0 acceptance의 blocker로 두지 않는다.
- 단, report/approval 판단에 필요한 preview image는 반드시 artifact로 남긴다.

---

## 7. 병렬 작업 분해

다른 세션에 나눠 맡길 때 아래 단위로 분리한다. 한 세션이 여러 영역의 파일을 동시에 소유하지 않게 한다.

### Session A: Worker Contract + Inspection

Owner files:

```text
worker/inspect_model.py
worker/app_job_contract.py
tests/test_worker_contract.py
docs/ELECTRON_UV_REVIEW_APP_MVP0_PRODUCTION_PLAN.ko.md
```

Tasks:

- `inspect_model` worker 구현
- mesh summary JSON schema 고정
- Blender import 지원: FBX, OBJ, GLB/GLTF
- role hint heuristic 구현
- sample pottery FBX로 smoke test
- JSON schema normalization helper 작성

Acceptance:

- sample FBX를 inspect하면 object name, vertices, edges, faces, uv_layers가 나온다.
- 실패 시 JSON error를 남긴다.
- stdout/stderr에 의존하지 않아도 결과를 읽을 수 있다.

### Session B: Low-poly Wrapper + Summary Normalization

Owner files:

```text
worker/run_app_retopo_job.py
worker/app_job_contract.py
tests/test_app_retopo_summary.py
```

Tasks:

- Electron API 입력을 기존 `worker/run_retopo_job.py` 호출로 변환
- `generation_report.json`, `validation_report.json`, `shape_report.json`을 `summary.json`으로 normalize
- `status.json` lifecycle 작성
- stdout/stderr log capture 위치 정의
- best-effort artifact 누락을 warnings로 변환

Acceptance:

- `generate_lowpoly` job 입력 하나로 project run folder가 만들어진다.
- 성공/실패 모두 `status.json`과 `summary.json` 또는 error JSON이 남는다.
- target/actual face count가 summary에 반드시 들어간다.

### Session C: Electron Main Project Service

Owner files:

```text
app/electron/main/*
app/shared/contracts/*
app/package.json
```

Tasks:

- Electron scaffold 생성
- project folder create/open
- source file copy
- IPC handlers:
  - `project:create`
  - `model:inspect`
  - `lowpoly:generate`
  - `lowpoly:approve`
  - `run:get`
- Blender executable path setting
- worker process spawn/cancel

Acceptance:

- Renderer 없이 main process unit/integration test에서 project 생성과 mock job 실행이 된다.
- project.json이 schema대로 갱신된다.
- job log와 status를 UI가 polling할 수 있다.

### Session D: Renderer MVP 0 UI

Owner files:

```text
app/electron/renderer/*
app/shared/contracts/*
```

Tasks:

- Project workspace UI 구현
- Import/Inspect flow
- Target setup form
- Job progress and log panel
- Report tabs
- Preview image 표시
- Approve flow

Acceptance:

- 사용자가 앱 안에서 source import -> inspect -> generate -> approve를 완료할 수 있다.
- target face count, actual face count, status, warnings가 보인다.
- 실패 job에서도 error panel이 보이고 앱이 멈추지 않는다.

### Session E: QA Fixtures + E2E Smoke

Owner files:

```text
tests/e2e/*
sample/
docs/MVP0_QA_RESULTS.ko.md
```

Tasks:

- small OBJ/FBX fixture 정의
- pottery sample smoke run 문서화
- Blender path 없는 환경의 skip 정책
- Electron e2e 또는 main-process integration smoke 작성
- acceptance checklist 실행 결과 기록

Acceptance:

- `pytest`가 Blender 없는 환경에서도 skip으로 통과한다.
- Blender가 있으면 inspect/generate smoke가 돈다.
- QA 결과 문서에 command, artifact path, pass/fail이 남는다.

---

## 8. 구현 순서

권장 순서:

1. `inspect_model` JSON contract 구현
2. project folder schema와 status lifecycle 구현
3. `generate_lowpoly` wrapper 구현
4. sample asset으로 worker smoke 통과
5. Electron main IPC/service 구현
6. Renderer MVP 0 UI 구현
7. approve/copy working model flow 구현
8. E2E smoke와 QA 결과 문서 작성

세션 병렬화 기준:

- 1~3은 backend contract가 먼저 안정되어야 한다.
- Renderer는 `app/shared/contracts` mock JSON을 기준으로 먼저 개발 가능하다.
- Electron main은 worker가 없더라도 mock runner로 먼저 개발 가능하다.
- QA는 sample과 expected artifact schema를 먼저 만들고, 실제 Blender smoke는 나중에 붙인다.

---

## 9. Production Acceptance Checklist

Functional:

- [ ] FBX import 후 object summary가 표시된다.
- [ ] OBJ import 후 object summary가 표시된다.
- [ ] GLB/GLTF import 후 object summary가 표시된다.
- [ ] high-poly로 판단된 source에서 low-poly generation job을 실행할 수 있다.
- [ ] existing low-poly로 판단된 source는 generation 없이 approve할 수 있다.
- [ ] target face count와 actual face count가 UI에 표시된다.
- [ ] topology validation status가 UI에 표시된다.
- [ ] shape report status가 UI에 표시된다.
- [ ] preview image가 표시된다.
- [ ] approve하면 `work/working_lowpoly.blend`가 생긴다.
- [ ] `project.json`에 approved run id가 기록된다.

Robustness:

- [ ] Blender executable path가 없으면 명확한 setup error를 보여준다.
- [ ] worker failure가 앱 crash로 이어지지 않는다.
- [ ] job cancel이 가능하거나 최소한 UI가 timeout/error로 복구된다.
- [ ] stdout/stderr log가 run folder에 저장된다.
- [ ] artifact 일부가 없어도 summary warnings로 표현된다.

Contract:

- [ ] 모든 worker command는 JSON input/output을 갖는다.
- [ ] 모든 run은 `status.json`을 갖는다.
- [ ] 모든 accepted run은 `summary.json`을 갖는다.
- [ ] Renderer는 worker stdout을 직접 parse하지 않는다.
- [ ] 이후 MVP가 읽을 `working_model` 경로가 project manifest에 있다.

Quality:

- [ ] Python tests 통과
- [ ] Electron typecheck 통과
- [ ] Electron renderer build 통과
- [ ] sample pottery smoke 결과 문서화

---

## 10. 위험 요소와 대응

### Blender import/export 편차

FBX/GLB import/export는 Blender 버전과 asset 상태에 따라 실패할 수 있다.

대응:

- import failure를 structured error로 반환한다.
- source file 확장자별로 import operator를 분기한다.
- unsupported material/animation은 MVP 0에서 warning 처리한다.

### 실제 high-poly가 너무 큰 경우

대형 OBJ/FBX는 background Blender memory/time cost가 크다.

대응:

- MVP 0 기본 target은 single object/single job으로 제한한다.
- max file size와 max face warning을 둔다.
- 대형 mesh proxy는 `worker/run_quad_retopo_job.py` 기반의 별도 post-MVP path로 격리한다.

### Low-poly 품질 판정 자동화 과신

shape/validation report가 accepted여도 artist가 보기엔 부족할 수 있다.

대응:

- report는 decision helper로만 사용한다.
- final gate는 사용자 approve로 둔다.
- rejected run도 history에 보존한다.

### Electron과 Python contract drift

UI와 worker가 각자 schema를 바꾸면 병렬 세션이 충돌한다.

대응:

- `app/shared/contracts` 또는 `worker/app_job_contract.py`에 schema version을 둔다.
- 추가 필드는 optional로만 도입한다.
- breaking change는 이 문서를 먼저 갱신한다.

---

## 11. Handoff Notes for Other Sessions

작업 시작 전 확인:

```bash
git status --short
rg "run_retopo_job|inspect_model|project.json|summary.json" .
```

현재 이 문서 작성 시점에서 PRD 파일은 untracked일 수 있다. 다른 세션은 원본 PRD를 덮어쓰지 말고 새 문서/새 코드 파일을 추가하는 방식으로 진행한다.

Conductor 병렬 작업 규칙:

- 각 세션은 자신의 owner files만 수정한다.
- shared contract를 바꿔야 하면 먼저 이 문서의 Worker API Contract와 Project Folder Contract를 갱신한다.
- `.context/`에는 세션별 smoke output, notes, 임시 파일을 저장해도 된다.
- generated asset이나 대형 output은 git에 추가하지 않는다.
- sample asset을 추가해야 하면 작은 fixture만 추가하고, 큰 asset은 `.context/attachments` 또는 별도 다운로드 절차로 문서화한다.

권장 브랜치/PR 설명:

```text
MVP0 productionizes the high-to-low preparation flow:
- adds stable worker JSON contracts for model inspection and low-poly generation
- records project/run artifacts in a local project folder
- exposes Electron main IPC for import, inspect, generate, and approve
- adds smoke coverage for sample FBX/OBJ flows
```

---

## 12. MVP 0 Done Definition

MVP 0은 다음 demo가 한 번에 성공하면 완료로 본다.

```text
1. App starts.
2. User creates or opens a project.
3. User imports sample/SM_Test_Pottery_a_02.fbx.
4. App inspects the mesh and shows object summary.
5. User chooses existing low-poly approve OR runs target 12000 low-poly generation.
6. App shows generation summary, topology report, shape report, preview.
7. User approves the result.
8. project.json points to work/working_lowpoly.blend.
9. The approved working model can be opened by the next MVP worker.
```

이 상태가 되면 MVP 1 UV Review App 세션은 `project.json`의 `working_model`만 읽고 기존 UV viewer/checker/report 작업을 시작할 수 있다.
