# Region-Aware Face UV Recovery 작업 계획서

> 대상: 다음 구현 세션(Opus).
> 배경: `IMPORTANT_REGION_UV_POLICY_PLAN.ko.md`의 post-split reject 방식은 human statue 얼굴에서 실패했다.
> 목표: 실패한 후반 reject 접근을 기본값에서 제외하고, 얼굴 개선을 segmentation/repair 앞단에서 다시 설계한다.

## 1. 현재 결론

직전 실험 결과:

```text
face smooth seams: 67 -> 71        worse
island count:       31 -> 58        over budget
worst distortion:   0.345 -> 3.074  much worse
```

hard correctness는 유지됐다.

```text
mandatory_90_missing == 0
mandatory_90_uv_unsplit == 0
raster_overlap <= threshold
uv_bounds_ok == true
fallback_used == false
```

하지만 핵심 목표인 얼굴 smooth seam 감소는 실패했다.

중요한 결론:

```text
post-split reject만으로는 얼굴 UV를 개선할 수 없다.
```

실패 원인:

```text
1. 얼굴 seam 대부분은 distortion refinement 단계가 아니라 segmentation 단계에서 이미 생김
2. face protected region을 후반에 막아도 이미 생긴 segmentation-born seam은 줄지 않음
3. 얼굴을 과도하게 하나의 protected island로 유지하면 distortion이 크게 증가함
4. overlap/correctness repair가 face region을 다시 자르면서 보호 정책이 무력화됨
```

## 2. 중요한 결정

### 2.1 post-split reject는 기본값에서 제외

기존 region policy의 post-split reject는 제품 기본값으로 쓰지 않는다.

유지할 것:

```text
region report
face smooth seam count
mandatory vs smooth seam 구분
protected split reject 기록
```

기본 off 또는 experimental로 둘 것:

```text
protected-region post-split reject
```

이유:

```text
얼굴 seam은 줄지 않았고, island count와 worst distortion이 악화됐다.
```

### 2.2 얼굴 개선은 앞단에서 해야 한다

다음 접근은 후반 reject가 아니라 다음 두 지점에 들어가야 한다.

```text
1. region-aware segmentation
2. region-aware overlap/fold repair reroute
```

즉:

```text
나중에 생긴 seam을 reject하지 말고,
처음부터 face-front smooth edge가 chart boundary가 되지 않도록 비용을 올린다.
```

## 3. 이번 작업의 목표

이번 milestone의 목표는 얼굴 UV를 완벽하게 만드는 것이 아니다.

목표:

```text
face-front smooth seam을 segmentation 단계에서 줄인다.
mandatory 90도 seam은 절대 제거하지 않는다.
얼굴이 하나의 무리한 island로 남아 worst distortion이 폭발하지 않게 한다.
overlap/fold repair가 face-front 내부를 다시 자르지 않도록 우회 비용을 적용한다.
기존 no-spec chart path는 그대로 유지한다.
```

성공해야 하는 방향:

```text
face front:
  중앙 smooth seam 감소
  눈/코/입 주변 checker 방향 전환 감소

face side/back/neck:
  필요한 보조 island 허용
  seam은 뒤통수/귀 뒤/목 아래/후드 경계로 유도
```

## 4. 비목표

이번 작업에서 하지 말 것:

- 새 UV 엔진 만들기
- low-poly 생성 수정
- `split_chart(...)` 전체 알고리즘을 대규모 교체
- 얼굴을 무조건 하나의 island로 유지
- `worst_island_distortion_max`를 몰래 완화해서 pass 처리
- post-split reject를 개선책의 중심으로 삼기

## 5. 설계 방향

### 5.1 Face Policy를 3구역으로 나눈다

얼굴을 하나의 protected region으로 취급하면 distortion이 폭발한다.

대신 다음처럼 나눈다.

```text
face_front_core:
  가장 중요한 정면 영역
  smooth seam 강하게 억제

face_side_transition:
  정면과 뒤쪽 사이
  필요한 seam 일부 허용

head_back_neck_preferred:
  seam 선호 영역
  뒤통수, 목 아래, 후드 뒤, 귀 뒤
```

정책:

```text
face_front_core:
  90도 미만 smooth seam cost 매우 높음
  mandatory 90도는 허용

face_side_transition:
  moderate cost
  distortion이 높으면 보조 seam 허용

head_back_neck_preferred:
  seam cost 낮음
  cut path가 이쪽으로 지나가도록 유도
```

### 5.2 Region-aware segmentation

현재 segmentation-born seam이 얼굴 smooth seam의 큰 원인이다.

따라서 `chart_uv_agent.segmentation` 쪽 split/merge/cut cost에 region 정보를 반영한다.

1차 구현은 다음 중 작은 범위로 한다.

```text
Option A:
  split_chart 결과가 face_front_core smooth edge를 boundary로 만들면 split 후보를 감점/거절

Option B:
  edge_cut_cost(...)에 region cost를 추가해서 face_front_core smooth edge를 비싸게 만든다

Option C:
  segmentation 후 auxiliary/merge 단계에서 face_front_core 내부 smooth boundary를 다시 merge 시도
```

권장 1차:

```text
Option B + C
```

이유:

```text
edge_cut_cost는 이미 fold-repair/reroute 개념과 맞고,
post-segmentation merge는 기존 solver를 크게 갈아엎지 않으면서 segmentation-born seam을 줄일 수 있다.
```

### 5.3 Region-aware repair reroute

overlap/fold correctness repair가 얼굴 내부를 다시 자르면 region policy가 무력화된다.

따라서 repair cut path는 다음 비용을 써야 한다.

```text
mandatory 90도 edge:
  cost near 0

face_front_core smooth edge:
  cost very high / forbidden unless no route exists

face_side_transition:
  medium cost

head_back_neck_preferred:
  low cost

user forbidden edge:
  infinite cost unless mandatory 90 conflict
```

목표:

```text
face 내부를 자르는 대신 뒤통수/목 아래/후드 뒤로 cut path를 보낸다.
```

### 5.4 얼굴을 하나로 유지하지 않는다

이번 실패에서 `worst distortion 3.074`가 나왔다.

따라서 다음 정책을 적용한다.

```text
face_front_core는 coherent하게 유지하되,
face_side/back/neck 쪽에는 보조 island를 허용한다.
```

즉 좋은 결과는 이것이다.

```text
bad:
  얼굴 전체를 하나의 island로 묶어서 distortion 폭발

good:
  얼굴 정면은 읽기 쉬운 island
  seam은 뒤/옆/목 아래로 빠짐
  distortion은 적당히 유지
```

## 6. 구현 제안

### 6.1 `artist_uv_agent/region_policy.py` 확장

기존 ImportantRegion 모델을 세분화한다.

```python
@dataclass
class ImportantRegion:
    name: str
    kind: str  # "face_front_core" | "face_side_transition" | "head_back_neck_preferred"
    face_ids: set[int]
    protected_edges: set[int]
    preferred_edges: set[int]
    smooth_seam_cost: float
    allow_auxiliary_seams: bool = True
```

추가 helper:

```python
def classify_face_regions(mesh, *, front_axis, up_axis) -> RegionPolicy:
    ...

def region_edge_cost_multiplier(edge_id, regions, mesh) -> float:
    ...

def region_boundary_audit(mesh, seams, regions) -> dict:
    ...
```

### 6.2 `chart_uv_agent.segmentation`에 region cost 연결

기존 함수를 대규모 교체하지 말고 optional parameter를 추가한다.

예:

```python
def edge_cut_cost(mesh, edge_id, *, forbidden=frozenset(), fold_angle=90.0, region_policy=None):
    ...
```

정책:

```text
if edge in face_front_core.protected_edges and dihedral < 90:
    cost *= 10~50

if edge in head_back_neck_preferred.preferred_edges:
    cost *= 0.25
```

주의:

```text
90도 이상 mandatory edge는 여전히 seam이다.
region cost가 mandatory를 이기면 안 된다.
```

### 6.3 post-segmentation protected merge

segmentation 후에 얼굴 내부 smooth seam을 줄이는 pass를 추가한다.

개념:

```text
for seam edge in face_front_core protected_edges:
  if edge.dihedral < 90:
    temporarily remove seam
    unwrap/evaluate or topology-audit
    if hard correctness remains safe and distortion does not explode:
      keep removed
    else:
      restore
```

1차는 Blender-free topology 기준만 써도 된다.

필수 조건:

```text
mandatory 90 seam 제거 금지
non-disk chart가 되면 제거 취소
overlap/unwrap 평가는 Blender run에서 최종 검증
```

### 6.4 repair reroute에 region cost 연결

`split_welded_folds` 또는 local cut path가 `edge_cut_cost`를 쓴다면 region-aware cost를 전달한다.

목표:

```text
face_front_core 내부 smooth edge를 지나가는 local cut path를 피한다.
head_back_neck_preferred로 우회한다.
```

## 7. Region Spec

UI가 아직 없으므로 JSON 기반으로 테스트한다.

예:

```json
{
  "version": 2,
  "enabled": true,
  "front_axis": "-Y",
  "up_axis": "+Z",
  "mode": "face_recovery",
  "regions": [
    {
      "name": "face_front_core",
      "kind": "face_front_core",
      "source": "heuristic",
      "smooth_seam_cost": 50.0
    },
    {
      "name": "face_side_transition",
      "kind": "face_side_transition",
      "source": "heuristic",
      "smooth_seam_cost": 5.0
    },
    {
      "name": "head_back_neck_preferred",
      "kind": "head_back_neck_preferred",
      "source": "heuristic",
      "smooth_seam_cost": 0.25
    }
  ]
}
```

No-spec behavior:

```text
--region-spec 없음:
  기존 chart solver와 동일해야 함
```

## 8. 테스트 기준

### 8.1 Baseline

비교 기준은 직전 성공한 base chart result다.

```text
.context/runs/rule_seam_core_blender/
```

기준 수치:

```text
baseline face smooth seams: 67
baseline island count: 31
baseline global stretch: 0.158
baseline worst island distortion: 0.345
```

주의:

직전 failed region-policy 결과는 baseline이 아니다.
그 결과는 실패 사례로만 사용한다.

### 8.2 Hard correctness

항상 유지:

```text
mandatory_90_missing == 0
mandatory_90_uv_unsplit == 0
raster_overlap_ratio <= 0.005
uv_bounds_ok == true
fallback_used == false
```

### 8.3 Face success

이번 milestone의 진짜 성공 기준:

```text
face smooth seams < 67
face_front_core smooth seams 감소
mandatory face seams는 별도 report
face closeup checker가 기존보다 덜 조각나 보임
```

권장 목표:

```text
face smooth seams: 67 -> 55 이하
```

55 이하가 안 되더라도, 최소한 67보다 줄어야 한다.

### 8.4 Regression budget

직전 실패에서 island count와 distortion이 크게 악화됐으므로 이번에는 명확한 budget을 둔다.

```text
global stretch <= baseline + 0.05
island count <= baseline + 10
worst island distortion <= 0.75
```

주의:

`worst island distortion <= 0.75`는 제품 threshold가 아니라 실험 안전장치다.
3.074 같은 실패를 다시 허용하지 않기 위한 guard다.

## 9. 테스트 계획

### 9.1 Pure tests

추가/수정:

```text
tests/test_region_policy.py
tests/test_chart_uv_minimal.py
```

테스트 항목:

```text
region edge cost:
  face_front_core smooth edge cost increases
  head_back_neck_preferred edge cost decreases
  mandatory 90 edge remains mandatory

protected merge:
  low-angle face_front seam can be removed
  mandatory 90 seam cannot be removed
  non-disk chart creation is rejected

no-spec path:
  region_policy=None preserves old behavior
```

### 9.2 Blender run

```bash
/Applications/Blender.app/Contents/MacOS/Blender --background \
  --python worker/run_quad_retopo_job.py -- \
  --p5-resume true \
  --uv-engine chart \
  --target-faces 5850 \
  --mesh-blend .context/runs/rule_seam_core_blender/adaptive_t5850.blend \
  --reference .context/attachments/8Btvr3/humanstatue_low.obj \
  --region-spec .context/region_specs/humanstatue_face_recovery.json \
  --out-dir .context/runs/region_aware_face_recovery
```

검증 스크립트:

```bash
uv run python - <<'PY'
import json, pathlib
d = pathlib.Path(".context/runs/region_aware_face_recovery")
gate = json.loads((d / "p5_gate.json").read_text())
report = json.loads((d / "seam_report.json").read_text())
regions = report.get("regions", [])
print("mandatory", gate["mandatory_90_missing"], gate["mandatory_90_uv_unsplit"])
print("islands", gate["final_island_count"])
print("stretch", gate["metrics"]["stretch_score"])
print("worst", gate["metrics"].get("worst_island_distortion"))
for r in regions:
    print(r)
PY
```

### 9.3 Visual inspection

필수 비교:

```text
before:
  .context/runs/rule_seam_core_blender/inspect/face_front.png
  .context/runs/rule_seam_core_blender/inspect/face_3q.png

after:
  .context/runs/region_aware_face_recovery/face_front_closeup.png
  .context/runs/region_aware_face_recovery/face_3q_closeup.png
```

눈으로 확인할 것:

```text
눈/코/입/턱 주변 checker 조각감 감소
얼굴 정면 중앙 smooth seam 감소
후드/뒤통수/목 아래 쪽 seam 유도 여부
robe/body/staff 품질 유지
```

## 10. 실패 조건

다음 중 하나라도 발생하면 이번 접근은 실패로 기록한다.

```text
face smooth seams >= 67
island count > baseline + 10
worst island distortion > 0.75
mandatory_90_missing > 0
mandatory_90_uv_unsplit > 0
robe/body checker 품질이 눈에 띄게 악화
```

실패 시 threshold를 완화하지 말고 report한다.

## 11. Opus에게 주는 핵심 지시

이전 post-split reject 실험을 반복하지 말 것.

해야 할 일:

```text
region-aware segmentation cost
region-aware fold/overlap repair reroute
face를 front/side/back-neck 3구역으로 나누기
face_front_core smooth seam 감소
mandatory seam 보존
no-spec path regression 방지
before/after closeup 비교
```

하지 말 것:

```text
얼굴 전체를 하나의 protected island로 묶기
post-split reject만으로 해결하려 하기
threshold 완화로 accepted 만들기
새 engine 만들기
base solver 재작성하기
```

## 12. 기대 결과

성공하면 다음 상태가 된다.

```text
base chart solver:
  몸/로브/스태프 품질 유지

region-aware face recovery:
  얼굴 정면 smooth seam 감소
  seam이 뒤/옆/목 아래로 더 잘 빠짐
  worst distortion 폭발 없음

review report:
  얼굴 seam 감소/유지/실패 이유를 수치와 closeup으로 설명
```

이게 Windows용 Nemotron 기반 3D modeling tool에서 “자동 UV지만 리뷰 가능한 결과”로 가는 다음 현실적인 단계다.
