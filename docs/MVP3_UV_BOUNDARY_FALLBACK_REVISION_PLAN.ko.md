# MVP 3 UV Boundary Fallback Revision Plan

> 대상: Opus 구현 세션  
> 변경 대상: MVP 3 Generate + Optimize 입력 정책  
> 기준 문서: `docs/ELECTRON_UV_REVIEW_APP_MVP3_PRODUCTION_PLAN.ko.md`, `docs/ELECTRON_UV_REVIEW_APP_MVP2_PRODUCTION_PLAN.ko.md`  
> 핵심 변경: Generate + Optimize에서 `active_user_seam_spec`이 없다는 이유만으로 막지 않는다. 기존 UV layer가 있으면 UV island boundary를 읽어 derived seam spec을 만들고, 그 spec으로 generate/optimize를 진행한다.

---

## 1. 왜 바꾸는가

현재 MVP 3 계획은 다음 전제를 갖고 있다.

```text
MVP 2 active_user_seam_spec
  -> Generate + Optimize
```

하지만 실제 사용자는 우리 seam editor를 쓰지 않았을 수 있다. Maya, Blender, 3ds Max, RizomUV, UVLayout 같은 다른 툴에서 이미 UV island/chapter를 나눠온 asset도 많다.

이 경우 Generate + Optimize에서 “seam을 선택하지 않았으니 실행 불가”라고 막는 것은 제품적으로 이상하다. 이미 존재하는 UV layout은 사용자의 seam/chapter 의도가 반영된 reference다.

따라서 MVP 3 입력 정책을 다음처럼 바꾼다.

```text
1. active_user_seam_spec 있음
   -> 그 spec을 source of truth로 사용

2. active_user_seam_spec 없음 + selected/active UV layer 있음
   -> 기존 UV island boundary를 derived seam spec으로 추출
   -> derived seam spec을 source of truth로 사용
   -> Generate + Optimize 실행

3. active_user_seam_spec 없음 + UV layer 없음
   -> 그때만 실행을 막고 Seam Editor 또는 UV 생성 준비로 안내
```

---

## 2. 제품 결정

### 2.1 Seam Editor는 필수 단계가 아니다

MVP 2 Seam Editor는 optional editor로 내려간다.

사용자 흐름은 둘 다 가능해야 한다.

```text
Flow A: 내부 편집
MVP 1 UV Review
  -> MVP 2 Seam Editor
  -> user_seam_spec 저장
  -> MVP 3 Generate + Optimize

Flow B: 외부 UV 사용
MVP 1 UV Review
  -> 기존 UV layer 확인
  -> MVP 3 Generate + Optimize
  -> app이 UV boundary derived seam spec 생성
```

### 2.2 Derived seam spec은 자동 seam 생성이 아니다

이 변경은 “앱이 새 seam을 결정한다”가 아니다.

앱은 이미 존재하는 UV island boundary를 읽어 `user_seam_edges`로 변환한다. 즉 source of truth는 여전히 사용자 또는 외부 DCC에서 만든 reference UV다.

### 2.3 Derived spec은 명시적으로 기록한다

Derived spec은 canonical `UserSeamSpec` schema를 따른다. 단, summary/report에는 출처를 반드시 남긴다.

```json
{
  "seam_source": {
    "type": "uv_boundary_derived",
    "path": "work/seams/derived_from_uv_boundary.json",
    "uv_layer": "UVChannel_1",
    "user_confirmed": false
  }
}
```

`user_confirmed=false`는 “MVP 2 editor에서 직접 저장한 spec은 아니지만, 기존 UV에서 파생했다”는 뜻이다. 실행을 막는 값이 아니다.

---

## 3. 문서 수정 지시

### 3.1 `docs/ELECTRON_UV_REVIEW_APP_MVP3_PRODUCTION_PLAN.ko.md`

다음 문구를 바꾼다.

Before:

```text
MVP 2 active_user_seam_spec
  -> spec validation
  -> user seam 기반 unwrap
```

After:

```text
seam source resolve
  -> active_user_seam_spec 있으면 사용
  -> 없으면 active UV layer boundary에서 derived seam spec 생성
  -> seam source validation
  -> user/reference seam 기반 unwrap
```

Before:

```text
- active_user_seam_spec이 없으면 실행하지 않고 명확한 setup error를 보여준다.
```

After:

```text
- active_user_seam_spec이 없더라도 selected/active UV layer가 있으면 UV island boundary를 derived seam spec으로 추출해 실행한다.
- active_user_seam_spec도 없고 usable UV layer도 없을 때만 setup error를 보여준다.
```

MVP 3 input list를 다음처럼 수정한다.

```text
1. working_model
2. selected_object
3. active_user_seam_spec optional
4. selected_uv_layer optional, fallback seam source용
5. latest_uv_review_run_id optional
```

Run folder에 다음 artifact를 추가한다.

```text
derived_from_uv_boundary.json optional
seam_source_resolution.json
```

Project state extension에 다음 optional pointer를 추가한다.

```json
{
  "latest_derived_seam_spec": "work/seams/derived_from_uv_boundary.json"
}
```

### 3.2 MVP 3 Generate UV Contract 수정

`generate_uv_from_seams` input에서 `seam_spec`을 optional로 바꾼다.

Before:

```json
{
  "seam_spec": "/absolute/path/to/project/work/seams/user_seam_spec.json"
}
```

After:

```json
{
  "seam_spec": "/absolute/path/to/project/work/seams/user_seam_spec.json",
  "uv_layer": "UVChannel_1",
  "seam_source_policy": "prefer_spec_then_uv_boundary"
}
```

Rules:

- `seam_spec`이 있으면 기존처럼 사용한다.
- `seam_spec`이 없고 `uv_layer`가 있으면 boundary extraction을 실행한다.
- `seam_spec`이 없고 `uv_layer`도 없으면 `status=needs_input`.

Output에 `seam_source` block을 추가한다.

```json
{
  "seam_source": {
    "type": "user_seam_spec | uv_boundary_derived",
    "path": "work/seams/user_seam_spec.json",
    "uv_layer": "UVChannel_1",
    "user_confirmed": true,
    "derived": false
  }
}
```

UV boundary fallback일 때:

```json
{
  "seam_source": {
    "type": "uv_boundary_derived",
    "path": "work/seams/derived_from_uv_boundary.json",
    "uv_layer": "UVChannel_1",
    "user_confirmed": false,
    "derived": true
  }
}
```

Failure output을 추가한다.

```json
{
  "status": "needs_input",
  "error": {
    "code": "missing_seam_source",
    "message": "No user seam spec or usable UV layer was found. Select a UV layer or create seams."
  }
}
```

### 3.3 MVP 2 문서의 handoff 표현 수정

`docs/ELECTRON_UV_REVIEW_APP_MVP2_PRODUCTION_PLAN.ko.md`에서 MVP 3 handoff는 필수 조건이 아니라 optional direct path로 바꾼다.

Before:

```text
work/seams/user_seam_spec.json is the primary MVP 3 input.
```

After:

```text
work/seams/user_seam_spec.json is the preferred explicit MVP 3 input.
If it is absent, MVP 3 may derive a seam spec from the selected UV layer boundary.
```

---

## 4. 구현 수정 지시

### 4.1 Worker: `worker/generate_uv_from_seams.py`

현재 worker가 seam spec missing을 hard failure로 처리하고 있다면 수정한다.

새 resolver 함수를 둔다.

```python
def resolve_seam_source(job, obj, mesh, project_dir):
    if job.get("seam_spec"):
        return load_and_validate_user_spec(...)

    uv_layer = job.get("uv_layer") or job.get("selected_uv_layer")
    if uv_layer:
        return derive_spec_from_uv_boundary(...)

    return needs_input("missing_seam_source")
```

Responsibilities:

- existing `seam_spec` path가 있으면 기존 behavior 유지
- path가 없으면 `uv_layer`를 확인
- UV layer가 있으면 `uv_agent.blender.uv_boundary` 또는 MVP 2 boundary extraction helper를 재사용
- derived spec을 `work/seams/derived_from_uv_boundary.json`에 저장
- run folder에도 copy 또는 report를 남김
- 이후 pipeline에는 일반 `UserSeamSpec`으로 전달

Do not:

- derived spec을 `active_user_seam_spec`으로 자동 덮어쓰기
- MVP 2의 `user_seam_spec.json`을 overwrite
- UV layer boundary 외의 새 seam을 추가

### 4.2 Worker contract: `worker/app_uv_generate_contract.py`

수정할 것:

- `seam_spec` required -> optional
- `uv_layer` optional 추가
- `seam_source_policy` 추가
- summary builder에 `seam_source` 추가
- `needs_input` status/code 추가

Status values:

```text
queued
running
accepted
needs_user_review
needs_input
failed
cancelled
```

### 4.3 Boundary extraction helper 재사용

이미 MVP 2에서 다음 기능이 있으면 재사용한다.

```text
uv_agent/geometry/uv_boundary.py
worker/seam_editor_worker.py extract_uv_boundary_as_seams
```

권장: worker 간 subprocess 호출보다 library helper로 분리해 재사용한다.

목표 API 예시:

```python
from uv_agent.blender.uv_boundary import extract_uv_boundary_edges

edge_ids, report = extract_uv_boundary_edges(obj, uv_layer_name)
spec = UserSeamSpec(
    object=obj.name,
    user_seam_edges=set(edge_ids),
    user_protected_edges=set(),
    notes=f"Derived from UV island boundaries: {uv_layer_name}",
)
```

### 4.4 Electron Main: `app/electron/main/uvGenerate*`

수정할 것:

- start input에서 `seamSpec`이 없어도 허용
- project의 `selected_uv_layer`를 worker job에 전달
- `active_user_seam_spec`이 없고 `selected_uv_layer`도 없으면 UI에 `needs_input` 반환
- accepted derived run이면 `latest_derived_seam_spec` pointer를 project.json에 기록할 수 있음
- 단, `active_user_seam_spec`을 자동으로 바꾸지는 않음

### 4.5 Renderer: `app/electron/renderer/uv-generate/*`

수정할 것:

- “Seam spec required” UI 제거
- input readiness panel을 “Seam Source”로 바꿈
- 세 가지 상태 표시

```text
Explicit seam spec: work/seams/user_seam_spec.json
Derived from UV boundary: UVChannel_1
Missing seam source: select a UV layer or create seams
```

- Generate button disable 조건:
  - working model 없음
  - selected object 없음
  - active seam spec 없음 AND selected UV layer 없음
- active seam spec 없음 BUT selected UV layer 있음이면 Generate enabled
- run summary에서 seam source type 표시

---

## 5. Contract Examples

### 5.1 Explicit seam spec path

Input:

```json
{
  "command": "generate_uv_from_seams",
  "model": "/project/work/working_lowpoly.blend",
  "object_name": "SM_Test_Pottery_a_02",
  "seam_spec": "/project/work/seams/user_seam_spec.json",
  "uv_layer": "UVChannel_1",
  "seam_source_policy": "prefer_spec_then_uv_boundary"
}
```

Summary:

```json
{
  "status": "accepted",
  "seam_source": {
    "type": "user_seam_spec",
    "path": "work/seams/user_seam_spec.json",
    "uv_layer": null,
    "user_confirmed": true,
    "derived": false
  }
}
```

### 5.2 UV boundary fallback path

Input:

```json
{
  "command": "generate_uv_from_seams",
  "model": "/project/work/working_lowpoly.blend",
  "object_name": "SM_Test_Pottery_a_02",
  "seam_spec": null,
  "uv_layer": "UVChannel_1",
  "seam_source_policy": "prefer_spec_then_uv_boundary"
}
```

Summary:

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
  },
  "seam_integrity": {
    "user_seam_count": 1230,
    "final_seam_count": 1230,
    "auto_added_seams": 0,
    "valid": true
  }
}
```

### 5.3 Missing both

Input:

```json
{
  "command": "generate_uv_from_seams",
  "model": "/project/work/working_lowpoly.blend",
  "object_name": "NoUVObject",
  "seam_spec": null,
  "uv_layer": null
}
```

Output:

```json
{
  "status": "needs_input",
  "error": {
    "code": "missing_seam_source",
    "message": "No user seam spec or usable UV layer was found. Select a UV layer or create seams."
  }
}
```

---

## 6. 테스트 계획

### 6.1 Unit tests

Add or update:

```text
tests/test_uv_generate_contract.py
tests/test_uv_generate_seam_integrity.py
tests/test_uv_boundary_extract.py
```

Required cases:

- explicit seam spec remains preferred over UV layer
- missing seam spec + valid UV layer creates derived seam spec
- missing seam spec + missing UV layer returns `needs_input`
- derived spec loads via `UserSeamSpec.from_dict`
- derived spec has `user_protected_edges=[]`
- generated summary includes `seam_source.type`
- `active_user_seam_spec` is not overwritten by derived fallback

### 6.2 Blender smoke

Use pottery or existing UV fixture.

Command shape:

```bash
blender --background --python worker/generate_uv_from_seams.py -- --job /tmp/job_uv_boundary_fallback.json
```

Job:

```json
{
  "command": "generate_uv_from_seams",
  "model": "/abs/project/work/working_lowpoly.blend",
  "object_name": "SM_Test_Pottery_a_02",
  "seam_spec": null,
  "uv_layer": "UVChannel_1",
  "options": {
    "auto_refine_user_seams": false,
    "repair_user_seams": false,
    "enforce_user_mandatory": false,
    "gate_user_mandatory": false,
    "optimize_layout": true
  }
}
```

Expected:

- `work/seams/derived_from_uv_boundary.json` exists
- `uv_generate_summary.json.seam_source.type == "uv_boundary_derived"`
- `auto_added_seams == 0`
- `final_seam_count == user_seam_count`
- selected UV output still generated

### 6.3 Electron tests

Update integration test:

- project has selected UV layer but no active seam spec
- Generate button is enabled
- worker job receives `uv_layer`
- accepted result records `seam_source.type=uv_boundary_derived`

---

## 7. Acceptance Criteria

Functional:

- [ ] Generate + Optimize no longer requires `active_user_seam_spec`.
- [ ] If selected UV layer exists, derived seam spec is generated and used.
- [ ] Explicit `active_user_seam_spec` still takes precedence.
- [ ] If neither seam spec nor UV layer exists, run returns `needs_input`.
- [ ] Derived spec is saved separately and does not overwrite user spec.
- [ ] Summary clearly records seam source.

Integrity:

- [ ] Derived boundary path still uses strict user/reference options.
- [ ] `auto_added_seams == 0` for derived boundary path.
- [ ] `final_seam_count == user_seam_count`.
- [ ] mandatory 90 diagnostics remain report-only.

UX:

- [ ] Generate UI shows “Explicit seam spec”, “Derived from UV boundary”, or “Missing seam source”.
- [ ] Generate is enabled when UV layer exists even if seam spec is missing.
- [ ] User is not forced into Seam Editor for already-UV’d assets.

---

## 8. Files Opus Should Touch

Primary:

```text
docs/ELECTRON_UV_REVIEW_APP_MVP3_PRODUCTION_PLAN.ko.md
worker/generate_uv_from_seams.py
worker/app_uv_generate_contract.py
app/electron/main/uvGenerate.ts
app/electron/main/project-service.ts
app/electron/renderer/src/uv-generate/
app/shared/contracts/uvGenerate.ts
tests/test_uv_generate_contract.py
tests/test_uv_generate_seam_integrity.py
app/test/integration.test.ts
```

Likely reusable helpers:

```text
uv_agent/geometry/uv_boundary.py
uv_agent/blender/uv_extract.py
worker/seam_editor_worker.py
```

Avoid touching unless needed:

```text
chart_uv_agent/pipeline.py
chart_uv_agent/layout_optimization.py
artist_uv_agent/user_seams.py
```

Reason: this is input resolution and app contract work, not UV algorithm work.

---

## 9. Implementation Order for Opus

1. Update MVP 3 plan text and contracts.
2. Update shared TypeScript/Python contract types.
3. Implement seam source resolver in `worker/generate_uv_from_seams.py`.
4. Reuse UV boundary extraction helper to write derived spec.
5. Add `seam_source` to summary/status.
6. Update Electron main to pass `selected_uv_layer` when seam spec is absent.
7. Update renderer readiness panel and Generate button logic.
8. Add unit/integration tests.
9. Run targeted tests.
10. Record result in a short QA note if Blender smoke is run.

---

## 10. Final Desired Behavior

The final product behavior should be:

```text
Case 1: User used our Seam Editor
  active_user_seam_spec exists
  -> Generate + Optimize uses it

Case 2: User brought UVs from external DCC
  no active_user_seam_spec
  selected UV layer exists
  -> Generate + Optimize derives seam spec from UV island boundary
  -> proceeds

Case 3: No seams and no UV
  no active_user_seam_spec
  no UV layer
  -> needs_input
  -> ask user to select UV layer, import UV'd model, or use Seam Editor
```

This makes Seam Editor optional and makes Generate + Optimize useful for real production assets that already have UV chapters from another tool.
