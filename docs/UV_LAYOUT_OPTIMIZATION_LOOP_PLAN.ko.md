# UV Layout Optimization Loop 작업 계획서

> 대상: Opus 4.8 구현 세션
> 목표: seam/chapter 생성 문제가 아니라, **이미 주어진 seam/reference UV island boundary를 기준으로 unwrap, relax, scale, rotate, pack을 반복 최적화**한다.
> 핵심 결정: user/reference seam mode에서는 기본적으로 `mandatory 90 seam rule`과 `mandatory UV hard gate`를 쓰지 않는다.

---

## 0. 반드시 먼저 읽을 컨텍스트

이 작업은 지금까지의 자동 seam 생성 실패를 반복하지 않기 위한 후단 작업이다.

관련 문서:

- `docs/USER_GUIDED_SEAM_UV_PIPELINE_PLAN.ko.md`
- `docs/RULE_BASED_UV_SEAM_CORE_PLAN.ko.md`
- `docs/CHART_UV_AGENT_PLAN.md`
- `docs/GENERIC_UV_REVISION_PLAN.md`
- `docs/UV_TRANSFER_PLAN.md`

최근 결론:

1. 완전 자동 seam/chapter 생성은 아직 artist-quality가 아니다.
2. 사용자가 직접 Mark Seam 하거나 reference UV island boundary를 제공하면 pipeline은 seam을 받아 unwrap/pack/report할 수 있다.
3. 그러나 현재 후단은 `unwrap_and_pack()` 한 번에 가까워서, UV layout 품질이 충분히 정돈됐다고 말하기 어렵다.
4. 따라서 지금 필요한 것은 seam을 더 자르는 게 아니라, **주어진 island를 더 잘 펴고, 같은 texel density로 맞추고, 회전/스케일/배치를 최적화하는 loop**다.

---

## 1. 최근 실험 결과

### 1.1 Human statue

`humanstatue_low.obj`의 reference UV boundary를 seam spec으로 추출해 user-seam path에 넣었다.

보정 있음:

- user seam: `864`
- auto-added seam: `147`
- final seam: `1077`
- gate: `accepted`
- island count: `80`
- stretch: `0.073863`
- worst island distortion: `0.217757`
- raster overlap: `0.000012`

보정 없음, strict:

- user seam: `864`
- auto-added seam: `0`
- final seam: `930` 또는 exact-only `864`
- gate: mandatory 기준 때문에 failed
- 실제 stretch/raster는 나쁘지 않음

결론:

- mandatory 90 rule을 강제로 적용하면 사용자가 준/reference seam set이 변경된다.
- user/reference seam mode에서는 이것을 기본으로 하면 안 된다.

### 1.2 Pottery FBX

첨부 파일:

```text
.context/attachments/qMjqaX/SM_Test_Pottery_a_02.fbx
```

대상 mesh:

- object: `SM_Test_Pottery_a_02`
- vertices: `6562`
- edges: `18701`
- faces: `12152`
- UV layer: `UVChannel_1`

실험:

1. FBX import
2. `Cube` 제거
3. `SM_Test_Pottery_a_02`만 clean blend/OBJ로 저장
4. `UVChannel_1`의 UV island boundary를 seam spec으로 추출
5. mandatory/hard 90 rule을 끄고 user-seam pipeline 실행

실행 옵션:

```bash
--auto-refine-user-seams false
--repair-user-seams false
--enforce-user-mandatory false
--gate-user-mandatory false
```

결과:

- gate: `accepted`
- user seam: `1230`
- auto-added seam: `0`
- final seam: `1230`
- island count: `52`
- stretch: `0.06866`
- worst island distortion: `0.202997`
- raster overlap: `0.0`
- packing efficiency: `0.583109`
- seam type: `{'user_seam': 1230}`

결과 파일:

```text
.context/runs/pottery_no_mandatory_rules/adaptive_t12152.blend
.context/runs/pottery_no_mandatory_rules/adaptive_t12152_uv.png
.context/runs/pottery_no_mandatory_rules/p5_gate.json
.context/runs/pottery_no_mandatory_rules/seam_report.json
.context/user_seam_specs/pottery_reference_uv_boundaries_no_rules.json
```

시각 판단:

- dome checker는 꽤 균일하다.
- base/ring/thin strip 부분은 더 정돈 가능하다.
- UV layout은 숫자상 accepted지만, “최대한 꽉 채운, 정돈된 final UV”라고 보긴 어렵다.
- 즉 **후단 최적화 loop가 필요하다.**

---

## 2. 제품 요구사항

사용자가 원하는 후단 동작:

> relax / rotate / scale을 조절하되, checker pattern이 최대한 늘어나지 않고, 모든 island의 texel density가 일치하며, 겹치는 부분 없이 한 UV tile을 꽉 채워 배치한다.

이 요구사항을 다음 네 가지 목표로 분해한다.

1. **Relax / unwrap 품질**
   - checker stretch를 최소화한다.
   - worst island distortion도 낮춘다.
   - 단, overlap/fold를 만들면 안 된다.

2. **Texel density 통일**
   - island별 UV area / 3D area 비율을 최대한 균일하게 만든다.
   - `texel_density_variance`를 낮춘다.

3. **Rotate / scale / pack 최적화**
   - island를 회전/스케일/이동하여 0-1 UV tile을 더 꽉 채운다.
   - packing efficiency를 높인다.
   - overlap은 0이어야 한다.

4. **비교 가능한 후보 탐색**
   - 한 번의 unwrap/pack 결과를 그대로 ship하지 않는다.
   - 여러 후보를 만들고, metric으로 best를 선택한다.

---

## 3. 중요한 제품 결정

### 3.1 User/reference seam mode에서는 90도 mandatory rule을 기본으로 쓰지 말 것

다음 두 룰은 **기본 사용 금지**다.

```text
mandatory 90 seam rule
mandatory UV hard gate
```

이유:

- 사용자/reference seam이 source of truth다.
- pipeline이 뒤에서 seam을 추가하면 사용자가 준 island/chapter 의도가 바뀐다.
- pottery 실험에서도 `mandatory_90_missing=85`, `mandatory_90_uv_unsplit=85`가 report로는 남지만, gate에서는 제외해야 했다.

따라서 user/reference seam mode 기본값:

```text
auto_refine_user_seams = false
repair_user_seams = false
enforce_user_mandatory = false
gate_user_mandatory = false
```

단, generic auto chart mode에서는 기존 mandatory rule을 당장 제거하지 말 것. 이번 작업 범위는 user/reference seam mode 후단 최적화다.

### 3.2 Seam을 더 자르지 말 것

이번 milestone은 seam optimization이 아니다.

금지:

- 새 seam 자동 추가
- chapter 재생성
- 90도 fold auxiliary repair
- distortion 때문에 island split
- protected/mandatory conflict resolver 수정

허용:

- 같은 seam set으로 unwrap 방식 후보 비교
- island UV 좌표 relax
- island scale normalize
- island rotation
- island packing
- metric 기반 best 선택

---

## 4. 현재 구현 상태

### 4.1 현재 unwrap path

핵심 파일:

```text
chart_uv_agent/unwrap.py
chart_uv_agent/pipeline.py
worker/run_quad_retopo_job.py
uv_agent/geometry/evaluation.py
```

현재 `chart_uv_agent/unwrap.py::unwrap_and_pack()`는 대략 다음 순서다.

```python
mark_seams(obj, seams)
bpy.ops.uv.unwrap(method="MINIMUM_STRETCH", margin=margin)
bpy.ops.uv.average_islands_scale()
bpy.ops.uv.pack_islands(rotate=True, margin=margin, shape_method="CONCAVE")
```

좋은 점:

- `MINIMUM_STRETCH`는 relax 계열이다.
- `average_islands_scale()`로 texel density 통일을 시도한다.
- pack에서 rotate가 켜져 있다.
- `raster_overlap_ratio`, `stretch_score`, `worst_island_distortion`, `texel_density_variance`, `packing_efficiency`를 측정한다.

부족한 점:

- unwrap 후보를 여러 개 비교하지 않는다.
- `ANGLE_BASED + minimize_stretch` 같은 대안을 시험하지 않는다.
- pack margin/shape/rotation 후보를 여러 개 비교하지 않는다.
- pack 결과가 나쁘면 다시 시도하지 않는다.
- density variance가 나빠도 반복 보정하지 않는다.
- 최종 layout score가 없다.

---

## 5. 구현 목표

새 기능 이름은 임시로 다음처럼 둔다.

```text
UV Layout Optimization Loop
```

구현은 기존 `chart_uv_agent` 안에 추가한다.

추천 파일:

```text
chart_uv_agent/layout_optimization.py
chart_uv_agent/unwrap.py
chart_uv_agent/pipeline.py
worker/run_quad_retopo_job.py
tests/test_uv_layout_optimization.py
```

---

## 6. 새 데이터 구조

### 6.1 `LayoutOptimizationConfig`

새 파일:

```text
chart_uv_agent/layout_optimization.py
```

예시:

```python
@dataclass(frozen=True)
class LayoutOptimizationConfig:
    enabled: bool = False
    mode: str = "user_reference"
    unwrap_methods: tuple[str, ...] = ("MINIMUM_STRETCH", "ANGLE_BASED")
    angle_based_minimize_iters: tuple[int, ...] = (0, 10, 30)
    margins: tuple[float, ...] = (0.002, 0.005, 0.01)
    pack_shapes: tuple[str, ...] = ("CONCAVE", "AABB")
    rotate_options: tuple[bool, ...] = (True,)
    average_scale: bool = True
    max_candidates: int = 24
    require_no_overlap: bool = True
    score_weights: dict[str, float] = ...
```

기본은 `enabled=False`로 둔다.

Opus가 기존 default path를 깨면 안 된다.

### 6.2 `LayoutCandidate`

```python
@dataclass
class LayoutCandidate:
    id: str
    unwrap_method: str
    minimize_iters: int
    margin: float
    pack_shape: str
    rotate: bool
    metrics: dict
    gate: dict | None
    score: float
    accepted: bool
    reason: str
```

### 6.3 `LayoutOptimizationResult`

```python
@dataclass
class LayoutOptimizationResult:
    selected_candidate_id: str
    candidates: list[LayoutCandidate]
    before_metrics: dict
    after_metrics: dict
    score_before: float
    score_after: float
```

JSON report에 그대로 들어가야 한다.

---

## 7. 핵심 알고리즘

### 7.1 입력

입력은 seam set이 이미 결정된 상태다.

```python
obj
mesh: MeshGraph
seams: set[int]
config: LayoutOptimizationConfig
```

절대 seam을 수정하지 않는다.

### 7.2 후보 생성

후보는 다음 축의 조합으로 만든다.

```text
unwrap method
relax/minimize iterations
average island scale on/off
pack shape
pack margin
rotate on/off
```

초기 후보 예시:

1. `MINIMUM_STRETCH`, no minimize, `CONCAVE`, margin `0.005`, rotate on
2. `MINIMUM_STRETCH`, no minimize, `CONCAVE`, margin `0.002`, rotate on
3. `MINIMUM_STRETCH`, no minimize, `AABB`, margin `0.005`, rotate on
4. `ANGLE_BASED`, minimize `10`, `CONCAVE`, margin `0.005`, rotate on
5. `ANGLE_BASED`, minimize `30`, `CONCAVE`, margin `0.005`, rotate on
6. `ANGLE_BASED`, minimize `30`, `AABB`, margin `0.002`, rotate on

주의:

- `MINIMUM_STRETCH`에는 현재처럼 Blender `minimize_stretch`를 붙이지 않는다.
- `ANGLE_BASED`에는 `bpy.ops.uv.minimize_stretch(iterations=N)`를 허용한다.
- `ANGLE_BASED + minimize_stretch`가 overlap/fold를 만들면 후보 탈락.

### 7.3 후보 적용

후보마다 다음을 실행한다.

```python
mark_seams(obj, seams)
unwrap(method=...)
if method == "ANGLE_BASED" and minimize_iters > 0:
    minimize_stretch(iterations=N)
average_islands_scale()
pack_islands(rotate=..., shape_method=..., margin=...)
read_uvmap()
evaluate_uv_solution()
raster_overlap_diagnosis()
```

Blender object에 UV를 계속 덮어쓰기 때문에, 각 후보의 UV snapshot을 저장할 방법이 필요하다.

권장:

- 후보 실행 후 `read_uvmap()`을 `UVMap`으로 저장
- best 후보가 정해지면 `write_uvmap_to_object()` 또는 후보 재실행으로 최종 UV 적용

만약 `write_uvmap_to_object()`가 없다면:

- 후보마다 metrics만 저장하고,
- best candidate config를 선택한 후 마지막에 한 번 더 같은 config로 unwrap/pack을 재실행한다.

초기 구현은 재실행 방식으로 충분하다.

### 7.4 후보 평가 score

hard reject:

```text
uv_bounds_ok == false
raster_overlap_ratio > raster_overlap_max
overlap_ratio > overlap_max
fallback_used == true
```

주의:

- user/reference seam mode에서는 `mandatory_90_missing`, `mandatory_90_uv_unsplit`를 reject 조건으로 쓰지 않는다.

score는 낮을수록 좋게 둔다.

예시:

```python
score =
    4.0 * stretch_score
  + 3.0 * worst_island_distortion
  + 2.0 * texel_density_variance
  + 2.0 * raster_overlap_ratio
  + 1.0 * overlap_ratio
  - 1.5 * packing_efficiency
  + 0.2 * small_island_ratio
```

설명:

- checker가 늘어나지 않는 것이 1순위다.
- worst island가 중요하다.
- texel density variance도 중요하다.
- packing efficiency는 높을수록 좋으므로 음수 가중치다.
- island count는 이번 loop가 seam을 바꾸지 않으므로 score에서 크게 보지 않는다.

### 7.5 Best 선택 규칙

1. hard reject 없는 후보만 남긴다.
2. score가 가장 낮은 후보를 선택한다.
3. 단, 기존 baseline보다 명확히 좋아지지 않으면 baseline을 유지한다.

baseline 유지 조건 예시:

```text
score 개선 < 1%
또는 packing은 좋아졌지만 stretch/worst가 유의미하게 나빠짐
```

구체 기준:

```text
stretch_score <= baseline_stretch * 1.05
worst_island_distortion <= baseline_worst * 1.05
texel_density_variance <= baseline_texel_var * 1.10
packing_efficiency >= baseline_packing - 0.02
```

이 조건을 만족하는 후보 중 score best를 고른다.

---

## 8. `unwrap.py` 변경

현재 `unwrap_and_pack()`는 옵션이 제한적이다.

추가/수정:

```python
def unwrap_and_pack(
    obj,
    seams,
    *,
    margin: float = 0.02,
    method: str = "MINIMUM_STRETCH",
    minimize_iters: int = 0,
    pack_shape: str = "CONCAVE",
    rotate: bool = True,
    average_scale: bool = True,
    layer_name: str = AI_UV_LAYER,
) -> int:
```

현재 `_pack()`은 이미 rotate를 받는다. `unwrap_and_pack()`에서 rotate를 전달하게 한다.

주의:

- 기존 호출부가 깨지면 안 된다.
- default는 현재와 동일해야 한다.

---

## 9. `pipeline.py` 변경

### 9.1 User-seam path에만 먼저 적용

첫 milestone은 `_run_user_seam_uv()`에만 적용한다.

이유:

- 지금 사용자가 원하는 것은 reference/user seam 기반 pipeline이다.
- generic auto chart path는 다른 규칙과 loop가 섞여 있어 blast radius가 크다.

새 인자:

```python
run_chart_uv(..., optimize_layout: bool = False, layout_optimization_config=None)
```

worker 인자:

```bash
--optimize-layout true
--layout-opt-preset user_reference
```

첫 구현에서는 preset 하나만 있어도 된다.

### 9.2 적용 위치

현재 `_run_user_seam_uv()` 안에는 `measure()`가 있다.

layout optimization은 final seam set이 확정된 뒤 실행한다.

순서:

```text
build user seam set
do not auto add seams
measure baseline
if optimize_layout:
    run layout optimization candidates on same final_seams
    apply best candidate UV
    re-measure final metrics/gate
build report
```

중요:

- `final_seams`가 바뀌면 안 된다.
- `auto_added_seams`는 계속 0이어야 한다.
- `seam_type_counts`는 그대로 유지되어야 한다.

---

## 10. Worker CLI

`worker/run_quad_retopo_job.py`에 옵션 추가:

```bash
--optimize-layout true|false
--layout-opt-preset user_reference
--layout-opt-max-candidates 24
```

그리고 user/reference seam mode 권장 실행 예시는 다음과 같다.

```bash
/Applications/Blender.app/Contents/MacOS/Blender --background \
  --python worker/run_quad_retopo_job.py -- \
  --p5-resume true \
  --uv-engine chart \
  --target-faces 12152 \
  --mesh-blend .context/runs/pottery_no_mandatory_rules/pottery_clean.blend \
  --reference .context/runs/pottery_no_mandatory_rules/pottery_reference.obj \
  --out-dir .context/runs/pottery_layout_opt \
  --user-seam-spec .context/user_seam_specs/pottery_reference_uv_boundaries_no_rules.json \
  --auto-refine-user-seams false \
  --repair-user-seams false \
  --enforce-user-mandatory false \
  --gate-user-mandatory false \
  --optimize-layout true \
  --layout-opt-preset user_reference
```

---

## 11. Report

`p5_gate.json`에 추가:

```json
"layout_optimization": {
  "enabled": true,
  "selected_candidate_id": "...",
  "score_before": 0.123,
  "score_after": 0.101,
  "before_metrics": {...},
  "after_metrics": {...},
  "candidates": [
    {
      "id": "slim_concave_m005",
      "unwrap_method": "MINIMUM_STRETCH",
      "minimize_iters": 0,
      "margin": 0.005,
      "pack_shape": "CONCAVE",
      "rotate": true,
      "metrics": {...},
      "score": 0.101,
      "accepted": true,
      "reason": "best_score"
    }
  ]
}
```

`seam_report.json`에는 최소한 summary만 넣는다.

```json
"layout_optimization": {
  "enabled": true,
  "selected_candidate_id": "...",
  "candidate_count": 12,
  "packing_efficiency_before": 0.583,
  "packing_efficiency_after": 0.641,
  "stretch_before": 0.068,
  "stretch_after": 0.071
}
```

---

## 12. 테스트 계획

### 12.1 Unit tests

새 파일:

```text
tests/test_uv_layout_optimization.py
```

Blender-free로 가능한 것:

1. score 계산
2. hard reject 판정
3. mandatory checks가 user/reference mode에서 reject 조건이 아닌지
4. best candidate 선택
5. baseline보다 나쁜 후보를 선택하지 않는지
6. config preset 생성

테스트 예시:

```python
def test_user_reference_score_ignores_mandatory_90_failures():
    metrics = {
        "mandatory_90_missing": 85,
        "mandatory_90_uv_unsplit": 85,
        "raster_overlap_ratio": 0.0,
        "overlap_ratio": 0.0,
        "stretch_score": 0.07,
        "worst_island_distortion": 0.20,
        "texel_density_variance": 0.001,
        "packing_efficiency": 0.58,
        "uv_bounds_ok": True,
        "fallback_used": False,
    }
    assert candidate_is_valid(metrics, mode="user_reference")
```

### 12.2 Blender integration test

Pottery asset로 실행:

```text
.context/attachments/qMjqaX/SM_Test_Pottery_a_02.fbx
```

검증:

- final seam count == user seam count
- auto_added_seams == 0
- mandatory gate checks not in blocking failures
- raster overlap == 0 또는 threshold 이하
- texel density variance 유지/개선
- packing efficiency baseline보다 개선 또는 stretch 악화 없이 유지
- UV layout PNG 생성
- checker render 생성

### 12.3 비교 baseline

기존 baseline:

```text
.context/runs/pottery_no_mandatory_rules/p5_gate.json
```

baseline 수치:

- packing efficiency: `0.583109`
- stretch: `0.06866`
- worst island distortion: `0.202997`
- raster overlap: `0.0`
- island count: `52`

성공 기준:

```text
packing_efficiency >= 0.583109
raster_overlap_ratio <= 0.005
stretch_score <= 0.06866 * 1.05
worst_island_distortion <= 0.202997 * 1.05
auto_added_seams == 0
final_seam_count == user_seam_count
```

packing이 개선되지 않더라도 stretch/worst가 더 좋아진 후보가 있으면 report하되, selected candidate는 score 기준으로 정직하게 선택한다.

---

## 13. 시각 검증

숫자만 보고 끝내면 안 된다.

반드시 확인할 파일:

```text
adaptive_t12152_uv.png
adaptive_t12152_generated_front_checker.png
adaptive_t12152_generated_side_checker.png
```

리뷰 기준:

- checker square가 dome에서 균일한가
- base/ring/thin strip에서 checker가 심하게 늘어나지 않는가
- tiny island가 과도하게 몰려 있지 않은가
- UV tile이 이전보다 더 꽉 차는가
- island 간 overlap이 없는가
- layout이 이전보다 더 읽기 쉬운가

주의:

- packing efficiency가 높아져도 checker가 망가지면 실패다.
- checker가 좋아져도 tile을 너무 비우면 후단 pack 최적화 목표는 달성하지 못한 것이다.

---

## 14. Done Criteria

작업 완료 조건:

1. `--optimize-layout true` 옵션이 동작한다.
2. user/reference seam mode에서 mandatory 90 seam rule과 mandatory UV hard gate를 기본으로 쓰지 않는다.
3. layout optimization은 seam set을 변경하지 않는다.
4. pottery FBX 기반 integration run이 생성된다.
5. report에 candidate별 metric과 selected candidate가 남는다.
6. final result가 baseline 대비 같거나 개선된다.
7. 관련 unit test가 통과한다.
8. 전체 테스트 또는 최소 관련 테스트가 green이다.

---

## 15. Non-goals

이번 작업에서 하지 말 것:

- 새 seam 자동 생성
- chapter 자동 생성
- mandatory 90 rule 재도입
- face/character-specific policy 추가
- UV island semantic naming
- reference UV slot similarity 최적화
- Smart UV fallback ship
- LLM/Nemotron edge 직접 선택

---

## 16. Opus에게 주는 핵심 지시

이번 작업의 목적은 “seam을 잘 나누는 것”이 아니다.

정확한 목적:

```text
이미 정해진 seam/reference boundary를 고정한 상태에서
relax / scale / rotate / pack 후보를 여러 개 만들고,
checker distortion, texel density, overlap, packing efficiency를 기준으로
가장 좋은 UV layout을 선택한다.
```

기본 user/reference seam mode에서는 다음을 쓰지 말 것:

```text
mandatory 90 seam rule
mandatory UV hard gate
```

이 둘은 report-only metric으로 남겨도 되지만, seam 추가나 gate fail의 기본 원인이 되면 안 된다.

최종적으로 사용자가 보고 싶은 것은:

```text
내가 준 seam/reference UV island boundary는 그대로 유지됐고,
checker가 덜 늘어나고,
texel density가 맞고,
overlap이 없고,
UV tile을 더 꽉 채운 결과
```

이다.
