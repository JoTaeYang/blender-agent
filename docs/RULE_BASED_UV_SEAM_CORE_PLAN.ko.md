# 규칙 기반 UV Seam Core 작업 계획서

> 대상: 다음 구현 세션(Opus 4.8).
> 목표: 기존 low-poly 생성 결과는 유지하고, UV seam 생성 품질을 개선한다.
> 결론: 완전 신규 UV 엔진을 만들지 말고, 기존 구현을 재사용하면서 seam 결정 코어를 분리한다.

## 1. 배경

현재 제품 목표는 다음 파이프라인이다.

```text
high polygon object 입력
  -> low polygon model 생성
  -> low polygon 기반 UV 생성
  -> 사용자가 검토 가능한 결과 출력
```

사용자 리뷰 결과, low-poly 생성은 1차로 만족한다. 문제는 UV seam 설계다.

리뷰어가 준 핵심 규칙은 다음 3개다.

1. 모델이 90도 이상 꺾이는 부분은 무조건 UV seam으로 자른다.
2. 90도 이하로 크게 꺾이지 않는 smooth surface에는 최대한 seam을 만들지 않는다.
3. UV island 개수는 최대한 줄인다. 단, checkerboard 매핑 후 왜곡이 일정 수치 이상이면 island 개수를 늘린다.

이 작업의 핵심은 “artist-style layout을 새로 발명하는 것”이 아니라, 위 3개 규칙을 안정적인 알고리즘과 검증 지표로 구현하는 것이다.

## 2. 중요한 결정

완전히 새로 만들지 않는다.

기존 구현에서 재사용할 것:

- `chart_uv_agent/segmentation.py`
  - 90도 이상 mandatory seam
  - chart flood fill
  - split/merge
  - fold boundary enforcement
- `chart_uv_agent/pipeline.py`
  - unwrap/refine/gate 흐름
- `uv_agent/geometry/evaluation.py`
  - stretch, angle distortion, texel density, packing 평가
  - `per_face_stretch`
- `artist_uv_agent/interactive_plan.py`
  - 사용자 승인/거절/수정 구조
  - forbidden/preferred seam intent 저장
- `artist_uv_agent/guided.py`
  - chapter/spec 기반 constraint 연결

새로 만들거나 분리할 것:

```text
artist_uv_agent/seam_policy.py
artist_uv_agent/seam_refinement.py
artist_uv_agent/seam_report.py
```

목표 구조:

```text
chart_uv_agent = geometry baseline
artist_uv_agent = user intent / review / reporting layer
new seam core = final seam decision layer
```

### 2.1 구현 엔트리포인트 명확화

Opus는 다음을 1차 구현 기준으로 삼는다.

```text
main runtime path:
  chart_uv_agent.pipeline.run_chart_uv(...)

worker / Blender entrypoint:
  기존 worker 경로를 유지하되, UV engine이 chart/minimal-distortion path를 호출하게 한다.
```

새로 추가하는 `artist_uv_agent/seam_policy.py`, `seam_refinement.py`, `seam_report.py`는 독립적인 신규 엔진이 아니다.
역할은 `run_chart_uv(...)`와 `chart_uv_agent` refinement가 사용할 수 있는 pure helper layer다.

즉, 첫 milestone에서는 다음을 하지 않는다.

```text
새 --uv-engine 이름 추가
기존 chart path 우회
artist_uv_agent를 default engine으로 승격
Windows UI 직접 구현
```

첫 milestone의 목표는 기존 chart/minimal-distortion path의 seam 결정과 report 품질을 개선하는 것이다.

### 2.2 현재 코드 기준선

`chart_uv_agent/pipeline.py`에는 이미 다음 성격의 코드가 존재한다.

- `run_chart_uv(...)`
- one-worst-island refinement loop
- `mandatory_90_missing` audit
- `mandatory_90_uv_unsplit` audit
- checker distortion report
- `shape_passes=False` 기본값
- `forbidden_edges` 인자

따라서 Opus는 “처음부터 새로 작성”하지 말고, 현재 구현을 먼저 읽은 뒤 다음 질문에 답해야 한다.

```text
1. 현재 구현이 계획서의 hard rule을 이미 만족하는가?
2. 만족한다면 report/schema/test만 보강하면 되는가?
3. 만족하지 않는다면 어느 단계에서 mandatory seam, forbidden edge, distortion loop가 깨지는가?
```

변경은 이 gap을 메우는 방향으로 제한한다.

## 3. 현재 구현에 대한 판단

### 유지할 것

`chart_uv_agent`는 이번 요구사항과 가장 잘 맞는다.

이미 코드 철학이 다음과 가깝다.

```text
90도 이상 fold는 mandatory seam
island 수는 최소화
distortion이 큰 chart만 추가 split
```

따라서 `chart_uv_agent`를 버리면 안 된다.

### 축소할 것

`artist_uv_agent/seams.py`의 part template 방식은 보조 힌트로만 사용한다.

기존 방식은 다음 케이스에는 좋다.

- cylinder
- staff/shaft
- strip
- cap
- 명확한 hard-surface part

하지만 human statue, robe, face, organic surface에서는 template이 과도한 seam을 만들 수 있다. 메인 판단 기준은 part type이 아니라 다음 순서여야 한다.

```text
mandatory 90도 fold
  -> user forbidden/preferred constraint
  -> distortion 측정
  -> visibility/hidden-side cost
  -> optional part template hint
```

### 제거하지 말 것

`artist_uv_agent/interactive_plan.py`는 유지한다.
사용자가 리뷰에서 “여기는 seam 만들지 마”, “여기는 잘라도 됨”을 남길 수 있는 구조가 제품적으로 중요하다.

## 4. 목표 동작

최종 UV seam solver는 다음 순서로 동작해야 한다.

```text
1. mesh 분석
2. 90도 이상 edge를 mandatory seam으로 지정
3. boundary / non-manifold / material boundary seam 후보 지정
4. user forbidden/preferred zone 적용
5. 초기 seam set 생성
6. unwrap
7. checker/stretch distortion 측정
8. distortion이 threshold 이하이면 종료
9. threshold 초과 island만 추가 seam 후보 탐색
10. 추가 seam 적용 후 distortion 개선량 측정
11. 개선량이 충분하면 채택, 아니면 되돌림
12. island cap 또는 품질 통과 시 종료
```

핵심 원칙:

```text
90도 이상은 반드시 자른다.
90도 미만은 기본적으로 자르지 않는다.
단, unwrap 결과가 나쁘면 가장 덜 보이는 경로를 찾아 추가로 자른다.
추가 seam은 distortion 개선량이 충분할 때만 채택한다.
```

## 5. 구현 단위

### 5.1 `artist_uv_agent/seam_policy.py`

목적: edge별 seam 의사결정에 필요한 score와 reason을 만든다.

필수 API 예시:

```python
@dataclass
class SeamPolicyConfig:
    mandatory_fold_angle: float = 90.0
    smooth_preserve_angle: float = 45.0
    distortion_threshold: float = 0.35
    min_improvement_ratio: float = 0.15
    max_islands: int = 80

@dataclass
class EdgeSeamDecision:
    edge_id: int
    decision: str  # "mandatory", "candidate", "forbidden", "ignored"
    score: float
    reasons: list[str]
```

정책:

- `dihedral >= 90`: mandatory seam
- boundary / non-manifold: seam
- material boundary: strong candidate
- user forbidden: seam 금지. 단, mandatory 90도와 충돌하면 mandatory가 이기고 conflict report에 기록
- user preferred: candidate score 감소
- smooth low-angle edge: candidate score 증가
- visible front/face/center area: candidate score 증가
- back/inside/underside/concave area: candidate score 감소

초기 구현에서 visibility 판단은 복잡한 semantic inference로 하지 않는다.

1차 허용:

```text
front_axis / up_axis가 명시된 경우:
  front-facing low-dihedral edge는 seam cost 증가
  back-facing edge는 seam cost 감소

명시된 axis가 없는 경우:
  visibility score는 neutral
```

`face`, `chest`, `inside arm` 같은 semantic zone은 LLM/interactive spec이 forbidden/preferred edge 또는 face set으로 넘긴 경우에만 적용한다.

`material boundary`는 mesh extractor에서 material id를 얻을 수 있을 때만 구현한다. 현재 mesh graph에 material 정보가 없다면 TODO/report-only로 남기고, 구현을 막지 않는다.

### 5.2 `artist_uv_agent/seam_refinement.py`

목적: unwrap 이후 distortion이 큰 island만 반복적으로 쪼갠다.

필수 흐름:

```text
input: mesh, initial_seams, uvmap, policy_config
loop:
  compute per-face stretch
  group stretch by island
  pick worst island
  if worst <= threshold: stop
  find candidate seam path inside that island
  apply candidate
  unwrap/re-evaluate
  keep only if improvement >= min_improvement_ratio
```

주의:

- 한 iteration에서 island 여러 개를 한꺼번에 자르지 않는다.
- 항상 worst island 하나만 처리한다.
- island 수가 늘었는데 distortion 개선이 작으면 revert한다.
- packing만 나쁘다고 seam을 늘리지 않는다.
- convexity만 나쁘다고 seam을 늘리지 않는다.
- overlap/fold 같은 hard correctness 이슈는 별도 repair로 처리한다.

`distortion_threshold`의 기본 의미는 global stretch가 아니라 **worst island distortion**으로 둔다.
Global stretch가 좋아도 특정 island 하나가 심하게 늘어나면 reviewer가 checkerboard에서 바로 보기 때문이다.

추천 판정:

```text
pass when:
  global_checker_distortion <= global_threshold
  and worst_island_distortion <= island_threshold

refine target:
  worst_island_distortion이 가장 큰 island
```

threshold naming이 이미 다른 코드와 충돌한다면 report에 alias를 명확히 남긴다.

```json
{
  "global_checker_distortion": 0.31,
  "worst_island_distortion": 0.48,
  "island_threshold": 0.35
}
```

### 5.3 `artist_uv_agent/seam_report.py`

목적: 결과를 사용자가 리뷰할 수 있게 설명한다.

필수 출력:

```json
{
  "mandatory_90_edges": 123,
  "mandatory_90_missing": 0,
  "initial_island_count": 34,
  "final_island_count": 42,
  "stretch_before": 0.52,
  "stretch_after": 0.31,
  "added_seams": [
    {
      "edge_id": 1234,
      "reason": ["distortion_repair", "hidden_side"],
      "island_before": 12,
      "improvement_ratio": 0.22
    }
  ],
  "conflicts": [
    {
      "edge_id": 3054,
      "user_rule": "forbidden",
      "engine_rule": "mandatory_90",
      "resolution": "mandatory_wins"
    }
  ]
}
```

이 report는 Windows 앱에서 seam overlay, distortion heatmap, reviewer feedback에 연결될 수 있어야 한다.

## 6. 기존 파일별 작업 지시

### 6.1 `chart_uv_agent/segmentation.py`

확인/수정할 것:

- `mandatory_seam_edges(mesh, fold_angle=90.0)`가 최종 seam set에서 절대 빠지지 않게 한다.
- absorb/merge/repair 과정에서 mandatory seam이 제거되지 않게 audit를 추가한다.
- `mandatory_seam_audit` 결과를 pipeline report에 포함한다.
- 90도 이상 edge가 chart 내부 slit로 남아 실제 UV boundary가 되지 않는 경우를 `enforce_fold_boundaries`로 처리한다.

성공 기준:

```text
mandatory_90_missing == 0
interior mandatory fold count == 0
```

### 6.2 `chart_uv_agent/pipeline.py`

확인/수정할 것:

- refinement loop를 명시적으로 distortion-driven으로 만든다.
- `per_face_stretch` 또는 island별 stretch를 기준으로 worst island를 고른다.
- 한 번에 하나의 worst island만 split한다.
- threshold 통과 즉시 종료한다.
- packing/convexity만으로 island를 늘리지 않는다.
- final report에 iteration history를 넣는다.

성공 기준:

```text
final_stretch <= threshold
또는 max_islands/max_iterations 도달 시 honest failure
```

### 6.3 `uv_agent/geometry/evaluation.py`

확인/수정할 것:

- per-face stretch가 안정적으로 계산되는지 확인한다.
- island별 stretch summary helper를 추가한다.
- area distortion, angle distortion, edge length distortion 중 최소 2개는 report에 포함한다.

추천 helper:

```python
def island_distortion_summary(mesh, uvmap, islands) -> list[dict]:
    ...
```

### 6.4 `artist_uv_agent/interactive_plan.py`

유지/확장할 것:

- approved/rejected/revise flow는 유지한다.
- chapter constraint에 다음 intent를 추가할 수 있게 한다.

```text
forbidden_edges
preferred_edges
forbidden_zones
preferred_zones
max_front_smooth_seams
mandatory_folds_must_split
distortion_threshold
max_island_count
```

사용자 리뷰가 다음 실행에 반영되는 구조가 필요하다.

## 7. Human Statue 테스트 기준

기준 파일:

```text
.context/interactive_applied/humanstatue_low_interactive.blend
```

이 파일 또는 동일 low-poly OBJ를 대상으로 다음 테스트를 수행한다.

Blender에서 `.blend`를 여는 경로가 불편하면 다음 OBJ를 우선 사용해도 된다.

```text
.context/attachments/8Btvr3/humanstatue_low.obj
.context/attachments/wWb5wc/humanstatue_low.obj
```

두 OBJ가 모두 존재하면 파일 크기/face count를 확인하고 현재 interactive 결과와 더 가까운 것을 사용한다. 어느 파일을 썼는지는 report에 반드시 기록한다.

### 필수 검증

- 90도 이상 edge가 전부 seam인지 확인
- 90도 이하 front/face smooth edge에 불필요한 seam이 줄었는지 확인
- checker distortion heatmap 생성
- island count와 stretch score 기록
- 추가 seam이 생겼다면 reason이 report에 남는지 확인

### 비교할 결과 3개

```text
Few Islands
Balanced
Low Distortion
```

각 preset은 같은 solver를 쓰되 threshold만 다르게 한다.

예시:

```text
Few Islands:
  island_threshold = 0.50
  global_threshold = 0.40
  max_islands = 50

Balanced:
  island_threshold = 0.35
  global_threshold = 0.30
  max_islands = 80

Low Distortion:
  island_threshold = 0.25
  global_threshold = 0.22
  max_islands = 120
```

위 수치는 calibration 시작점이다. 통과를 위해 임의로 완화하지 말고, 실패하면 실패한 preset으로 report한다.

## 8. Windows 앱 관점 요구사항

최종 엔진은 완전 자동 black box가 되면 안 된다. 리뷰 가능한 자동화여야 한다.

앱에서 필요한 viewport/debug layer:

- 90도 이상 mandatory seam 표시
- 최종 generated seam 표시
- 추가 distortion repair seam 표시
- forbidden/preferred edge 표시
- checker distortion heatmap
- island별 stretch score
- seam reason tooltip 또는 side panel
- 사용자 override 저장

최소 산출물:

```text
final .blend/.fbx/.obj
uv_report.json
seam_report.json
distortion_heatmap.png
seam_overlay.png
```

이번 구현 세션에서 Windows UI를 만들 필요는 없다.

1차 산출물은 앱이 나중에 소비할 수 있는 파일과 JSON schema다.
이미지 생성이 Blender/headless 환경에서 실패하면 JSON report와 SVG overlay를 우선 남기고, 실패 사유를 기록한다.

## 9. LLM/Nemotron의 역할

LLM이 edge 하나하나를 직접 고르게 하지 않는다.

LLM/Nemotron의 역할:

- 모델 타입 추론: character/statue/hard-surface/prop 등
- forbidden/preferred zone 제안
- 사용자 피드백을 constraint로 변환
- preset/threshold 추천
- report 설명 생성

실제 seam 선택은 deterministic geometry solver가 한다.

권장 구조:

```text
Nemotron:
  "이 모델은 human statue다"
  "얼굴/가슴 정면 seam 회피"
  "팔 안쪽/다리 안쪽/등/발바닥 선호"

Geometry solver:
  edge angle 계산
  mandatory seam 적용
  candidate score 계산
  unwrap
  distortion 평가
  iterative refinement
```

## 10. 성공 기준

1차 성공 기준:

- `mandatory_90_missing == 0`
- 90도 미만 smooth surface seam 수가 기존 interactive 결과보다 감소
- checker/stretch score가 threshold 이하이거나, 실패 시 어느 island가 문제인지 report
- final UV overlap/raster overlap이 gate 통과
- UV가 `[0, 1]` 범위에 있음
- Smart UV fallback을 최종 결과로 사용하지 않음
- seam별 reason report가 생성됨

“seam”은 단순히 seam edge id set에 들어가는 것을 의미하지 않는다.
90도 이상 edge는 실제 exported UV에서 양쪽 face가 분리되어 있어야 한다.

따라서 둘 다 통과해야 한다.

```text
mandatory_90_missing == 0        # seam set audit
mandatory_90_uv_unsplit == 0     # exported UV audit
```

2차 성공 기준:

- 사용자가 forbidden/preferred edge를 지정하면 다음 run에 반영됨
- `Few Islands`, `Balanced`, `Low Distortion` preset 비교 가능
- human statue에서 얼굴/정면 smooth seam이 눈에 띄게 줄어듦
- 추가 seam은 distortion 개선량이 report로 설명됨

## 11. 비목표

이번 작업에서 하지 말 것:

- low-poly 생성 로직 대규모 수정
- 완전히 새로운 UV package 생성
- reference UV transfer 고도화
- semantic artist layout grammar를 메인 목표로 삼기
- packing 효율만 올리기 위해 seam을 늘리기
- convexity 점수만 올리기 위해 seam을 늘리기
- LLM이 직접 edge-level seam을 결정하게 만들기

## 12. 권장 작업 순서

1. 현재 `chart_uv_agent/pipeline.py`와 `chart_uv_agent/segmentation.py`가 이미 만족하는 항목을 먼저 audit한다.
2. 현재 report에 `mandatory_90_missing`, `mandatory_90_uv_unsplit`, `checker_distortion`, `worst_island_distortion`이 있는지 확인한다.
3. 빠진 test를 추가한다. 특히 final seam set과 exported UV 양쪽을 검증한다.
4. island별 distortion summary helper를 추가하거나 기존 `_distortion_report`를 확장한다.
5. distortion-driven worst-island refinement loop가 이미 충분하면 재작성하지 않는다. 부족한 경우에만 `seam_refinement.py`로 분리한다.
6. forbidden/preferred/visibility/cost 계산이 흩어져 있으면 `seam_policy.py`로 분리한다.
7. seam reason과 iteration history가 부족하면 `seam_report.py`로 schema를 정리한다.
8. human statue 기준으로 `Few Islands`, `Balanced`, `Low Distortion` 3개 preset을 비교한다.
9. 결과 이미지와 report를 `.context/` 아래에 저장한다.
10. 어떤 규칙이 통과/실패했는지 최종 요약한다.

## 13. Opus에게 주는 핵심 지시

이 작업은 “새 UV 엔진 만들기”가 아니다.

핵심은 다음이다.

```text
기존 chart_uv_agent의 geometry 기반 UV 생성은 살린다.
artist_uv_agent의 interactive/user intent 구조도 살린다.
하지만 seam 결정 기준은 새 Seam Decision Core로 분리한다.

90도 이상은 hard rule.
90도 미만은 preserve bias.
distortion이 threshold를 넘을 때만 추가 seam.
추가 seam은 개선량이 충분할 때만 채택.
모든 seam은 reason을 남긴다.
```

최종적으로 사용자가 봐야 하는 것은 “UV가 나왔다”가 아니라:

```text
왜 여기를 잘랐는지
왜 여기는 자르지 않았는지
checker 왜곡이 얼마나 줄었는지
island 수가 왜 늘었는지
사용자 리뷰가 다음 결과에 반영됐는지
```

이다.
