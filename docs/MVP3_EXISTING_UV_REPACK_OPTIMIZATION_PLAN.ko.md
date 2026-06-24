# MVP 3 Existing UV Repack/Optimization 보강 계획서

> 대상: Opus 구현 세션  
> 범위: `/app`의 `Generate + Optimize` 단계와 Python/Blender UV pipeline  
> 핵심 목표: **기존 UV 레이어의 island boundary를 seam source로 쓰는 현재 방향은 유지**하되, 그 seam으로 UV를 다시 펴고(texel density normalize), island를 회전/스케일/배치하여 실제로 눈에 띄게 정돈된 UV layout을 만든다.

---

## 0. 현재 진단

실제 프로젝트 런:

```text
project: /Users/yangdole/UVReviewProjects/SM_Test_Pottery_a_02_3
uv run: runs/uv_run_cbff1548-29c4-43b0-a768-e93963482be1
export: exports/export_c2916abe-b2a9-484f-89fd-9fc8ec02494e
```

확인된 사실:

- Blender worker가 실제로 돌았다. mock이 아니다.
- Export는 `work/uv/selected_uv.blend`를 열어 `UVChannel_1`을 내보냈다.
- Export 단계는 UV unwrap/pack/relax를 하지 않는다.
- MVP 3는 `active_user_seam_spec` 없이 `selected_uv_layer = UVChannel_1`에서 seam을 derive했다.
- seam source는 `uv_boundary_derived`이고, derived boundary edge count는 `724`다.
- Generate + Optimize는 24개 후보를 돌렸지만 개선 폭이 작다.

실제 개선:

```text
packing_efficiency: 0.583109 -> 0.591278
stretch:            0.06866  -> 0.06866
kept_baseline:      false
selected:           slim_concave_m002
```

결론:

```text
기존 UV를 source로 쓰는 흐름은 맞다.
문제는 "기존 UV boundary seam으로 다시 펴고 배치하는 후단 최적화"가 너무 약하다는 점이다.
```

---

## 1. 반드시 유지할 제품 원칙

### 1.1 기존 UV boundary를 seam source로 쓰는 것은 맞다

`active_user_seam_spec`이 없고 `selected_uv_layer`가 있으면 다음 흐름이 기본이다.

```text
selected UV layer
  -> UV island boundary edge 추출
  -> derived seam spec 생성
  -> seam set 고정
  -> re-unwrap / density normalize / rotate / pack
  -> best layout 선택
  -> selected_uv.blend 저장
```

이 fallback은 제거하지 않는다.

### 1.2 seam set은 기본적으로 바꾸지 않는다

이번 작업의 목표는 seam generation이 아니다.

금지:

- 자동 seam 추가
- derived seam spec을 사용자 spec으로 덮어쓰기
- mandatory 90 seam을 강제로 추가
- overlap을 피하려고 몰래 island split

허용:

- 같은 seam set으로 다시 unwrap
- island texel density 정규화
- island 회전/스케일/이동
- 더 강한 custom packing
- 품질이 나쁘면 repair suggestion만 생성

### 1.3 Export는 계속 pass-through로 둔다

Export 단계가 UV를 고치면 provenance가 흐려진다.

Export는:

- `work/uv/selected_uv.blend`를 열고
- active UV layer를 선택하고
- requested format으로 내보내고
- re-open validation만 한다.

UV 정리는 전부 MVP 3에서 끝나야 한다.

---

## 2. 구현 목표

### 목표 A: 기존 UV boundary 추출 품질 확인 및 보강

현재 실제 런에서는 기존 pottery 문서의 `1230` seam과 달리 derived boundary가 `724`다.

할 일:

1. `uv_agent.blender.uv_extract.extract_uv_boundary_edges`와 `uv_agent.geometry.uv_boundary` 경로를 점검한다.
2. UV island boundary edge count가 왜 `724`인지 설명 가능한 report를 남긴다.
3. 같은 모델/레이어에서 Blender UV island count와 boundary edge count가 일관되는지 테스트한다.
4. boundary 추출이 partial이면 수정한다.

수정 후보 파일:

```text
uv_agent/blender/uv_extract.py
uv_agent/geometry/uv_boundary.py
worker/generate_uv_from_seams.py
tests/test_uv_boundary_extract.py
tests/test_uv_generate_seam_integrity.py
```

완료 기준:

- `seam_source_resolution.json`에 다음 값이 포함된다.

```json
{
  "uv_layer": "UVChannel_1",
  "island_count": 52,
  "boundary_edge_count": 724,
  "boundary_extraction_method": "...",
  "dropped_or_ambiguous_edges": []
}
```

- boundary edge count가 적은 경우에도 왜 적은지 report가 설명한다.

### 목표 B: island-level UV layout optimizer 추가

현재 `chart_uv_agent.layout_optimization`은 `unwrap_and_pack()` 후보만 반복한다. Blender 기본 `pack_islands` 차이가 작아서 결과가 거의 안 바뀐다.

새 후단을 추가한다.

```text
fixed seam set
  -> unwrap candidates
  -> read UV islands
  -> per-island density normalize
  -> island orientation candidates
  -> custom pack candidates
  -> write candidate UV back to Blender
  -> evaluate metrics
  -> choose best
```

수정 후보 파일:

```text
chart_uv_agent/layout_optimization.py
chart_uv_agent/pipeline.py
chart_uv_agent/unwrap.py
artist_uv_agent/layout.py
uv_agent/geometry/packing.py
uv_agent/geometry/uv_review.py
```

구현 세부:

1. `read_uvmap` 결과를 island 단위로 group한다.
2. island별 bbox, area, 3D area, density를 계산한다.
3. island별 scale을 전체 평균 texel density에 맞춘다.
4. long strip/ring island는 orientation pass를 적용한다.
5. `uv_agent.geometry.packing.pack_islands`의 maxrects/shelf pack을 Blender UV 좌표에 적용한다.
6. Blender `pack_islands` 후보와 custom pack 후보를 모두 candidate table에 넣는다.

새 후보 예시:

```text
slim_blender_concave_m002
slim_blender_aabb_m002
slim_custom_maxrects_m002
slim_custom_shelf_m002
slim_custom_orient_maxrects_m002
abf_custom_maxrects_m002_min10
```

완료 기준:

- pottery 기준 packing efficiency가 최소 `0.65` 이상으로 오른다.
- overlap/raster overlap은 계속 `0` 또는 gate threshold 이하.
- stretch/worst distortion은 baseline 대비 5% 이상 악화되지 않는다.
- candidate_summary에 custom pack 후보가 기록된다.

### 목표 C: scoring을 시각 품질 중심으로 조정

현재 score는 개선폭이 작아도 candidate를 선택한다. 사용자가 보기에는 차이가 없다.

할 일:

1. `packing_efficiency` 목표를 더 강하게 반영한다.
2. `texel_density_variance` 악화를 강하게 reject한다.
3. `stretch`가 같고 packing만 미세하게 오른 후보는 `minor_improvement`로 표시한다.
4. 의미 있는 개선 기준을 둔다.

권장 기준:

```text
meaningful if:
  packing_efficiency_after >= packing_efficiency_before + 0.05
  OR texel_density_variance_after <= texel_density_variance_before * 0.75
  OR score improvement >= 10%
```

candidate_summary 확장:

```json
{
  "improvement": {
    "meaningful": true,
    "packing_delta": 0.08,
    "stretch_delta": 0.001,
    "texel_density_delta": -0.0003
  }
}
```

수정 후보 파일:

```text
chart_uv_agent/layout_optimization.py
worker/app_uv_generate_contract.py
app/shared/contracts/uvGenerate.ts
app/electron/renderer/src/uv-generate/UvGenerateWorkspace.tsx
```

### 목표 D: UI가 "최적화가 실제로 됐는지" 정직하게 보여주게 하기

현재 UI는 selected candidate만 보여줘서 사용자가 개선폭을 판단하기 어렵다.

Generate + Optimize 우측 패널에 다음을 보여준다.

```text
Source: Existing UV boundary (UVChannel_1)
Derived seams: 724
Optimization:
  Packing 0.583 -> 0.591 (+1.4%) [Minor]
  Stretch 0.06866 -> 0.06866 [Unchanged]
  Result: No meaningful visual improvement
```

상태 문구:

- `Meaningful improvement`
- `Minor packing-only improvement`
- `Baseline retained`
- `Needs better packing`
- `Consider seam edits`

수정 후보 파일:

```text
app/electron/renderer/src/uv-generate/UvGenerateWorkspace.tsx
app/electron/renderer/src/i18n/strings.ts
app/electron/renderer/src/styles.css
app/shared/contracts/uvGenerate.ts
```

---

## 3. 구현 순서

### Step 1. Reproduce 현재 pottery 상태

명령/앱 실행으로 다음 파일을 다시 만든다.

```text
runs/<uv_run_id>/uv_generate_summary.json
runs/<uv_run_id>/candidate_summary.json
runs/<uv_run_id>/seam_source_resolution.json
runs/<uv_run_id>/selected_uv_layout.png
runs/<uv_run_id>/baseline_uv_layout.png
```

기준값:

```text
boundary_edge_count = 724
packing before = 0.583109
packing after = 0.591278
stretch unchanged = 0.06866
```

### Step 2. Boundary extraction report 보강

먼저 UV boundary seam source가 정확히 무엇인지 투명하게 만든다.

출력:

```text
seam_source_resolution.json
derived_from_uv_boundary.json
```

추가 필드:

- `island_count`
- `uv_layer_loop_count`
- `boundary_edge_count`
- `ambiguous_boundary_count`
- `mesh_boundary_edge_count`
- `method`

### Step 3. Custom pack adapter 구현

`uv_agent.geometry.packing.pack_islands`는 geometry-level packer다. Blender UV map에 적용하는 adapter를 만든다.

새 함수 후보:

```python
def repack_uv_islands_custom(obj, mesh, uvmap, *, padding, algorithm, allow_rotate):
    ...
```

위치 후보:

```text
chart_uv_agent/unwrap.py
```

또는 새 파일:

```text
chart_uv_agent/island_layout.py
```

### Step 4. Layout candidate 확장

`chart_uv_agent/layout_optimization.py::candidate_specs`에 pack backend를 추가한다.

예:

```python
pack_backend: "blender" | "maxrects" | "shelf"
orient_long_islands: bool
density_normalize: bool
```

`LayoutCandidate.to_dict()`와 TS contract에도 필드를 추가한다.

### Step 5. Pipeline에 적용

`chart_uv_agent/pipeline.py::_run_user_seam_uv`의 `_measure_candidate`가 다음을 실행하도록 바꾼다.

```text
unwrap_and_pack(..., pack_backend="blender")
if candidate.pack_backend != "blender":
    read_uvmap
    normalize density
    orient islands
    custom pack
    write_uvmap
evaluate
```

주의:

- candidate 평가마다 object UV가 덮어써진다.
- 최종 선택 후 selected spec을 반드시 다시 적용한다.
- baseline preview는 selected 적용 후 다시 baseline을 재적용하는 현재 방식을 유지한다.

### Step 6. UI/summary 업데이트

`uv_generate_summary.json`에 improvement block을 추가한다.

렌더러는 다음을 표시한다.

- seam source: `existing UV boundary`
- derived seam count
- candidate count
- packing delta
- stretch delta
- meaningful/minor/no improvement verdict

### Step 7. QA

필수 테스트:

```bash
pytest tests/test_uv_generate_candidates.py
pytest tests/test_uv_generate_contract.py
pytest tests/test_uv_boundary_extract.py
npm --prefix app run typecheck
npm --prefix app run test:integration
```

Blender e2e:

```bash
pytest tests/e2e/test_mvp3_uv_generate.py
pytest tests/e2e/test_mvp5_export.py
```

Pottery acceptance:

```text
status = accepted
seam_source.type = uv_boundary_derived
auto_added_seams = 0
final_seam_count == user_seam_count
raster_overlap_ratio <= 0.005
uv_bounds_ok = true
packing_efficiency_after >= 0.65
stretch_after <= stretch_before * 1.05
candidate_summary includes custom pack backend candidates
UI says meaningful/minor improvement correctly
export manifest source.uv_generate_run_id == latest accepted UV run
```

---

## 4. Non-goals

이번 작업에서 하지 말 것:

- Export 단계에서 UV를 고치기
- 기존 UV 레이어를 그대로 copy만 해서 accepted 처리하기
- seam을 몰래 추가해 packing efficiency를 올리기
- `active_user_seam_spec`을 derived spec으로 overwrite하기
- threshold를 완화해서 보기만 좋게 만들기
- mock 결과로 QA 통과 처리하기

---

## 5. Opus에게 줄 구현 요약

```text
MVP 3 Generate + Optimize는 기존 UV layer boundary를 seam source로 쓰는 현재 방향이 맞다.
하지만 현재 optimizer는 Blender pack 후보 몇 개만 돌려 실제 개선이 거의 없다.

작업 목표:
1. selected UV layer boundary extraction report를 강화한다.
2. fixed seam set으로 다시 unwrap한 뒤 island-level density normalize / orientation / custom maxrects or shelf packing을 적용한다.
3. candidate_summary에 pack_backend / orientation / improvement 정보를 기록한다.
4. UI에서 "의미 있는 개선인지"를 정직하게 보여준다.
5. Export는 그대로 selected_uv.blend pass-through로 둔다.

성공 기준:
Pottery 기준 packing_efficiency를 0.583 -> 최소 0.65 이상으로 올리되,
overlap은 0 또는 threshold 이하, stretch는 5% 이상 악화되지 않아야 한다.
```
