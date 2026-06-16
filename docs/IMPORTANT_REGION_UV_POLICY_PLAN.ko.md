# 중요 부위 UV 정책 작업 계획서

> 대상: 다음 구현 세션(Opus).
> 전제: `RULE_BASED_UV_SEAM_CORE_PLAN.ko.md`의 base seam solver는 유지한다.
> 목표: 얼굴/손/로고/정면 패널처럼 텍스처 작업자가 중요하게 보는 부위에 별도 seam 정책을 적용한다.

## 1. 배경

현재 rule-based chart UV는 다음 핵심 규칙을 구현했다.

```text
1. 90도 이상 꺾이는 edge는 mandatory seam
2. 90도 미만 smooth surface에는 seam을 최대한 만들지 않음
3. checker/stretch distortion이 기준을 넘을 때만 island를 추가
4. 추가 seam은 개선량이 충분할 때만 채택
5. seam_report.json으로 이유를 설명
```

Blender 검증 결과, human statue 기준으로 robe/body/staff/trident는 꽤 양호하다.
하지만 얼굴은 아직 부족하다.

얼굴 문제는 단순히 distortion 수치 문제가 아니다.

현재 얼굴은:

- 눈/코/입/턱 주변 checker 방향이 자주 끊김
- 작은 island/방향 전환이 많음
- texture artist가 직접 칠하기에는 face front가 덜 coherent함
- generic distortion solver 입장에서는 합리적이어도 artist 입장에서는 불편할 수 있음

따라서 다음 단계는 base solver를 바꾸는 것이 아니라, 그 위에 **Important Region Policy**를 얹는 것이다.

## 2. 핵심 결정

새 UV 엔진을 만들지 않는다.

유지할 것:

```text
chart_uv_agent.pipeline.run_chart_uv(...)
mandatory_90_missing / mandatory_90_uv_unsplit audit
distortion-driven split
accept/revert
seam_report.json
```

추가할 것:

```text
important region detection / spec
protected-region seam cost
face-front preserve policy
region-aware distortion split rejection/reroute
region report
```

### 2.1 1차 구현 엔트리포인트

Opus는 다음 runtime path를 유지한다.

```text
chart_uv_agent.pipeline.run_chart_uv(...)
worker/run_quad_retopo_job.py 의 chart P5 path
```

새 region policy는 독립 엔진이 아니다.
`run_chart_uv(...)`에 optional input으로 들어가는 보조 정책이어야 한다.

권장 signature:

```python
def run_chart_uv(..., region_policy=None, ...):
    ...
```

worker는 1차로 JSON 파일을 받아 넘길 수 있게 한다.

```text
--region-spec path/to/region_spec.json
```

`--region-spec`가 없으면 기존 동작과 결과가 최대한 동일해야 한다.

즉 구조는 다음과 같다.

```text
Base Rule Solver
  - 90도 mandatory seam
  - distortion threshold
  - minimal islands

Important Region Policy
  - face/front/hands/logo 등 중요 부위 보호
  - seam 위치를 back/inside/underside로 유도
  - 중요 부위에서는 coherence를 distortion보다 우선

User Review Layer
  - protected/preferred region 수정
  - 다음 run에 반영
```

## 3. 이번 작업의 목표

1차 목표는 **face/front region**이다.

Human statue 기준으로 다음을 달성해야 한다.

```text
얼굴 정면 smooth seam 감소
얼굴 중앙부 checker 방향 전환 감소
얼굴 정면을 가능한 한 coherent island로 유지
90도 이상 hard fold는 여전히 mandatory seam
seam이 필요하면 뒤통수/후드 뒤/목 아래 쪽으로 유도
```

중요한 tradeoff:

```text
일반 부위:
  distortion 개선을 위해 island 추가 허용

얼굴 정면:
  약간의 distortion은 허용하되, smooth seam 남발을 막음
```

## 4. 비목표

이번 작업에서 하지 말 것:

- low-poly 생성 로직 수정
- base chart solver 전체 재작성
- 새 `--uv-engine` 추가
- LLM이 edge-level seam을 직접 선택하게 만들기
- 얼굴을 완벽한 production artist UV로 만드는 것
- 모든 character anatomy segmentation을 한 번에 해결하기

이번 목표는 “generic chart UV에 중요한 부위 보호 정책을 얹는 1차 버전”이다.

## 5. 구현 제안

### 5.1 새 데이터 모델

새 모듈을 추가한다.

```text
artist_uv_agent/region_policy.py
```

필수 데이터 구조 예:

```python
@dataclass
class ImportantRegion:
    name: str
    kind: str  # "face_front" | "hand" | "logo" | "front_panel" | "generic"
    face_ids: set[int]
    protected_edges: set[int]
    preferred_edges: set[int]
    forbidden_smooth_angle: float = 90.0
    distortion_priority: str = "secondary"  # "primary" | "secondary"
    max_smooth_seams: int | None = None

@dataclass
class RegionPolicyConfig:
    front_axis: str = ""
    up_axis: str = ""
    face_front_normal_threshold: float = 0.25
    face_upper_body_z_min: float = 0.55
    smooth_seam_angle_max: float = 45.0
    protected_cost_multiplier: float = 10.0
```

1차 구현에서는 face detection이 완벽할 필요는 없다.
다음 두 경로를 모두 지원한다.

```text
1. explicit face_ids / edge_ids가 있으면 그것을 우선 사용
2. 없으면 heuristic으로 face_front 후보를 만든다
```

중요: 기본 axis는 비워둔다.
axis를 모르는 상태에서 자동으로 `+Z` 같은 값을 가정하지 않는다.

현재 human statue Blender 결과는 시각 검사 기준으로 다음 축을 우선 사용한다.

```text
front_axis = "-Y"
up_axis = "+Z"
```

다른 asset에서는 UI/Nemotron/사용자가 축을 넘겨주는 방식으로 처리한다.

### 5.2 Face Front 후보 자동 탐지

Human/statue 계열의 1차 heuristic:

```text
front_axis 기준으로 정면을 향하는 face
up_axis 기준으로 상단 55~90% 높이에 있는 face
mesh 중심 근처의 face
작은 staff/trident 같은 별도 돌출물은 제외
```

정확하지 않아도 된다. 중요한 것은 report에 “자동 탐지 영역”을 명확히 남기는 것이다.

1차 구현에서 heuristic이 애매하면 실패로 보지 않는다.
대신 `confidence: "low"`로 report하고, explicit `face_ids` 또는 `protected_edges` 입력을 우선 지원한다.

출력 예:

```json
{
  "regions": [
    {
      "name": "face_front_auto",
      "kind": "face_front",
      "face_count": 312,
      "protected_edge_count": 184,
      "detection": "heuristic",
      "front_axis": "+Z",
      "up_axis": "+Y"
    }
  ]
}
```

주의:

- staff/trident가 얼굴 근처에 있으면 heuristic이 섞일 수 있다.
- 이 경우 region report에 confidence를 낮게 기록한다.
- 추후 Nemotron/UI가 face_ids를 넘겨주는 방식으로 대체 가능해야 한다.

### 5.3 Seam Policy와 연결

기존 `artist_uv_agent/seam_policy.py`에 region-aware cost를 추가한다.

정책:

```text
edge가 region protected_edges에 속함
  and edge.dihedral < 90도:
    seam 금지 또는 매우 높은 cost

edge가 region preferred_edges에 속함:
    seam cost 낮춤

edge가 90도 이상:
    무조건 mandatory seam
    protected region과 충돌하면 mandatory wins + conflict report
```

즉, 중요한 원칙은 그대로다.

```text
mandatory 90도 > region protection > distortion split
```

### 5.4 Distortion Split과 연결

기존 `chart_uv_agent.pipeline.run_chart_uv(...)`의 distortion split 단계에서 region policy를 사용한다.

기존:

```text
worst island 선택
split_chart(...)
accept/revert by improvement ratio
```

추가:

```text
candidate split이 protected region smooth edge를 많이 자르면 reject
```

1차 구현에서는 split path를 정교하게 reroute하지 않는다.
`split_chart(...)` 자체를 cost-aware 알고리즘으로 바꾸지 않는다.

이번 milestone에서 해야 하는 것은 **post-split reject**다.

```text
protected face_front를 자르는 split이면:
  - 추가 seam edge 중 protected smooth edge 수를 계산
  - count > 0이면 해당 provisional split을 reject/revert
  - history에 "protected_region_reject" 기록
```

예:

```json
{
  "round": 6,
  "action": "reject",
  "reason": "protected_region_reject",
  "region": "face_front_auto",
  "protected_edges_cut": [123, 456],
  "split_island": 7
}
```

### 5.5 Region Report

`seam_report.json` 또는 별도 `region_report.json`에 다음을 포함한다.

추천은 `seam_report.json` 안에 `regions` block을 추가하는 것이다.

```json
{
  "regions": [
    {
      "name": "face_front_auto",
      "kind": "face_front",
      "face_count": 312,
      "protected_edge_count": 184,
      "smooth_seams_in_region": 3,
      "mandatory_seams_in_region": 12,
      "rejected_splits": 2,
      "status": "protected_with_mandatory_conflicts"
    }
  ]
}
```

필수 구분:

```text
mandatory_seams_in_region:
  90도 이상이라 어쩔 수 없이 자른 seam

smooth_seams_in_region:
  90도 미만인데 region 안에서 생긴 seam
  이 값이 낮아야 함
```

이 구분이 없으면 reviewer가 “왜 얼굴에 seam이 있냐”고 했을 때 설명할 수 없다.

### 5.6 Region Spec JSON

1차 구현은 UI가 없으므로 JSON 입력을 지원한다.

예:

```json
{
  "version": 1,
  "enabled": true,
  "front_axis": "-Y",
  "up_axis": "+Z",
  "regions": [
    {
      "name": "face_front",
      "kind": "face_front",
      "source": "heuristic",
      "face_ids": [],
      "protected_edges": [],
      "preferred_edges": []
    }
  ]
}
```

규칙:

```text
enabled=false 또는 region-spec 없음:
  기존 chart solver와 동일하게 동작

face_ids/protected_edges가 비어 있음:
  heuristic으로 region 후보 생성

face_ids/protected_edges가 제공됨:
  heuristic보다 우선
```

## 6. Nemotron 역할

Nemotron은 edge를 직접 고르지 않는다.

Nemotron이 할 일:

```text
모델 타입 추론:
  human/statue/character/prop/hard-surface

중요 부위 제안:
  face, hands, chest logo, front robe, weapon handle

region intent 생성:
  face_front는 protected
  head_back/neck_under는 preferred seam zone

사용자 피드백 변환:
  "얼굴 가운데 seam 만들지 마" -> face_front protected region
  "목 뒤로 seam 보내" -> neck_back preferred region
```

Geometry solver가 할 일:

```text
face/edge set 계산
mandatory seam 적용
candidate cost 계산
distortion split
accept/revert
report 생성
```

## 7. Windows 앱 UI 요구사항

이번 구현은 UI를 만들 필요는 없다.
하지만 앱이 소비할 수 있는 JSON과 overlay 산출물을 준비해야 한다.

나중에 앱에서 보여줄 정보:

```text
Face protected region overlay
Mandatory seams in face
Smooth seams in face
Rejected protected-region splits
Preferred seam zones
Checker distortion heatmap
```

최소 산출물:

```text
p5_gate.json
seam_report.json
region_report.json 또는 seam_report.regions
face_region_overlay.png 또는 svg
face_checker_closeup.png
```

이미지 산출물이 headless Blender에서 어렵다면 JSON을 우선한다.
`face_checker_closeup.png`는 있으면 좋지만, 1차 완료 조건은 아니다.
Blender run에서는 최소 `seam_report.json`의 `regions` block이 있어야 한다.

## 8. 테스트 대상

기준 asset:

```text
.context/runs/rule_seam_core_blender/adaptive_t5850.blend
.context/interactive_applied/humanstatue_low_interactive.blend
.context/attachments/8Btvr3/humanstatue_low.obj
```

우선순위:

1. 현재 rule seam core 결과 `.context/runs/rule_seam_core_blender/adaptive_t5850.blend`
2. 기존 interactive 결과 `.context/interactive_applied/humanstatue_low_interactive.blend`
3. OBJ만 필요한 pure test는 `.context/attachments/8Btvr3/humanstatue_low.obj`

## 9. 성공 기준

### 9.1 Hard correctness

기존 hard rule은 절대 깨지면 안 된다.

```text
mandatory_90_missing == 0
mandatory_90_uv_unsplit == 0
raster_overlap_ratio <= 0.005
uv_bounds_ok == true
fallback_used == false
```

### 9.2 Face policy

Human statue 기준:

```text
face_front smooth seams 감소
face_front protected split reject 기록 존재
mandatory face seams와 smooth face seams가 분리 reporting됨
얼굴 정면 checker 방향 전환이 기존 결과보다 줄어듦
얼굴 정면이 더 coherent island로 보임
```

정량 지표 예:

```text
smooth_seams_in_face_front <= previous_result
protected_region_rejected_splits is reported
mandatory_seams_in_face_front reported separately
```

주의: `protected_region_rejected_splits >= 1`을 hard success 기준으로 삼지 않는다.
후보 split이 protected face를 건드리지 않았다면 reject가 0일 수 있다. 이 경우도 정상이다.

정성 지표:

```text
face closeup checker render에서 눈/코/입 주변 조각감이 줄어야 함
```

### 9.3 Regression 방지

얼굴을 보호하느라 전체 UV가 망가지면 안 된다.

허용 범위:

```text
global stretch가 크게 악화되지 않음 (권장: 기존 대비 +0.05 이내)
robe/body checker 품질 유지
island count가 과도하게 증가하지 않음 (권장: 기존 대비 +10 이내)
staff/trident strip 품질 유지
```

## 10. 테스트 계획

### 10.1 Pure tests

새 테스트 파일:

```text
tests/test_region_policy.py
```

테스트 항목:

```text
face_front heuristic이 face_ids/protected_edges를 생성한다
90도 mandatory edge는 protected region 안에서도 mandatory다
90도 미만 protected edge는 forbidden/high-cost 처리된다
region conflict report가 생성된다
protected split reject record가 만들어진다
region-spec 없음이면 기존 chart solver 결과가 유지된다
```

### 10.2 Blender run

Blender worker로 P5 chart run을 실행한다.

예:

```bash
/Applications/Blender.app/Contents/MacOS/Blender --background \
  --python worker/run_quad_retopo_job.py -- \
  --p5-resume true \
  --uv-engine chart \
  --target-faces 5850 \
  --mesh-blend .context/runs/rule_seam_core_blender/adaptive_t5850.blend \
  --reference .context/attachments/8Btvr3/humanstatue_low.obj \
  --region-spec .context/region_specs/humanstatue_face_front.json \
  --out-dir .context/runs/important_region_face_policy
```

검증:

```bash
uv run python - <<'PY'
import json, pathlib
d = pathlib.Path(".context/runs/important_region_face_policy")
gate = json.loads((d / "p5_gate.json").read_text())
report = json.loads((d / "seam_report.json").read_text())
print(gate["mandatory_90_missing"], gate["mandatory_90_uv_unsplit"])
print(report.get("regions", []))
PY
```

### 10.3 Visual inspection

필수 산출:

```text
face_front_closeup.png
face_3q_closeup.png
face_region_overlay.png
```

비교:

```text
before: .context/runs/rule_seam_core_blender/inspect/face_front.png
after:  .context/runs/important_region_face_policy/face_front_closeup.png
```

## 11. 주의할 점

1. 얼굴을 보호한다고 90도 mandatory seam을 제거하면 안 된다.
2. face heuristic이 틀릴 수 있으니 report에 confidence/source를 남긴다.
3. protected region 때문에 distortion이 남으면 honest best-effort로 보고한다.
4. `gate=accepted`를 만들기 위해 threshold를 몰래 완화하지 않는다.
5. region policy는 optional이어야 한다. 일반 prop/hard-surface에서는 꺼질 수 있어야 한다.
6. `split_chart(...)`를 대규모로 재작성하지 않는다. 1차는 post-split reject만 한다.
7. axis를 추측하지 않는다. human statue 테스트는 `front_axis="-Y"`, `up_axis="+Z"`를 명시한다.

## 12. Opus에게 주는 핵심 지시

이 작업은 얼굴용 새 UV 엔진을 만드는 것이 아니다.

해야 할 일:

```text
기존 rule-based chart solver 유지
ImportantRegion/RegionPolicy 추가
face_front protected region 1차 구현
protected smooth seam을 줄이도록 split reject/cost 적용
mandatory 90도 seam은 계속 우선
seam_report에 region별 설명 추가
Blender closeup으로 before/after 비교
```

최종적으로 보고해야 할 것:

```text
face_front smooth seam이 얼마나 줄었는지
mandatory seam은 몇 개인지
protected region 때문에 reject된 split은 몇 개인지
전체 UV hard gate가 유지됐는지
얼굴 closeup checker가 실제로 나아졌는지
```

## 13. 기대 결과

이 작업 후에도 얼굴 UV가 완벽한 production artist UV가 되지는 않을 수 있다.

하지만 최소한 다음 상태가 되어야 한다.

```text
generic chart UV:
  몸/로브/스태프에 강함

important region policy:
  얼굴/손/로고 같은 중요 부위의 seam 남발을 줄임

review workflow:
  사용자가 왜 seam이 생겼는지, 왜 보호됐는지, 어디가 실패했는지 알 수 있음
```

이 방향이 Windows용 Nemotron 기반 3D modeler tool에 가장 현실적이다.
