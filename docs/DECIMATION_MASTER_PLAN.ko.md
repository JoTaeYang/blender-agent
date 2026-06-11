# ZBrush식 Decimation Optimize 개선 계획서

## 1. 배경

현재 `decimation_optimize` 모드는 Blender `Decimate(COLLAPSE)` modifier를 중심으로 D1-D4까지 구현되어 있다.

구현된 범위:

- D1: Decimate Collapse 기반 target ratio search
- D2: decimation 전용 shape preservation 평가
- D3: hard edge / boundary feature vertex group 보존
- D4: triangulate, auto smooth, weighted normal, optional normal transfer

`sample/anchor.obj` 테스트 결과:

```text
source faces: 9,828,143
target faces: 2,000
actual faces: 8,008
band: failed
shape status: accepted
normal cleanup: applied
```

추가 ratio sweep 결과, Blender Collapse는 이 모델에서 `ratio=0`까지 내려도 `8008 faces / 3246 verts`에 머문다.

```text
ratio 0.0002035 -> 8008 faces
ratio 0.00015   -> 8008 faces
ratio 0.0001    -> 8008 faces
ratio 0.000075  -> 8008 faces
ratio 0.00005   -> 8008 faces
ratio 0.000025  -> 8008 faces
ratio 0.00001   -> 8008 faces
ratio 0         -> 8008 faces
```

저장된 lowpoly 검사:

```text
verts: 3246
edges: 11515
faces: 8008
face sizes: all triangles
components: 25
boundary edges: 954
non-manifold edges: 1747
```

결론: 단순 Blender Collapse modifier만으로는 ZBrush Decimation Master처럼 target polygon budget을 강하게 만족시키기 어렵다. 특히 non-manifold, boundary, detached component가 많은 입력에서는 Blender Collapse가 topology constraint 하한에 걸릴 수 있다.

## 2. 목표

목표는 Blender에서 ZBrush Decimation Master를 완전히 복제하는 것이 아니라, 그 핵심 동작에 가까운 decimation pipeline을 구현하는 것이다.

핵심 목표:

- target face count를 강하게 추적한다.
- triangle 기반 결과를 정상 결과로 인정한다.
- flat / low-importance 영역을 더 많이 줄인다.
- silhouette, hard edge, boundary, curvature, material/UV seam은 더 보존한다.
- n-gon은 만들지 않는다.
- shape error와 normal artifact를 정량 평가한다.
- modifier plateau를 명확히 감지하고 같은 decimation family 안에서 다음 전략으로 넘어간다.

비목표:

- quad retopo edge flow 생성
- animation deformation용 topology 생성
- Blender 기본 Decimate modifier만으로 모든 target을 보장
- voxel remesh나 cluster remesh를 기본 decimation 결과로 사용하는 것

voxel/cluster fallback은 `strict_target` 최후 fallback으로 둘 수 있지만, ZBrush식 decimation의 주 경로로 보지 않는다.

## 3. 개선된 파이프라인

```text
High-poly mesh
  -> preprocess / topology diagnosis
  -> feature and importance map
  -> primary Blender Collapse pass
  -> plateau detection
  -> progressive decimation retry
       - cleanup constraints
       - component budget policy
       - relaxed feature protection
       - planar/flat-region reduction
  -> optional custom QEM triangle collapse
  -> shape projection / normal cleanup
  -> validation / reports
  -> export
```

## 4. Phase DM1. Plateau Detection and Reporting

### 목적

현재는 Collapse가 target을 못 맞춰도 단순히 `failed`만 남긴다. 먼저 modifier가 target 하한에 걸렸음을 시스템이 명확히 감지해야 한다.

### 구현

- `search_decimate_ratio`에 plateau 감지 추가
  - ratio가 감소했는데 face count가 동일하거나 거의 동일하면 plateau 후보
  - 동일 face count가 2회 이상 반복되면 조기 중단
  - `min_ratio`에 도달한 경우와 Blender 자체 plateau를 구분
- `SearchResult`에 메타데이터 추가
  - `history`
  - `stopped_reason`
  - `plateau_face_count`
  - `plateau_ratio`
  - `hit_min_ratio`
- `generation_report.json`에 plateau 정보를 기록

### 완료 기준

`anchor.obj -> 2000` 실행 시 report가 다음을 설명해야 한다.

```json
{
  "band": "failed",
  "stopped_reason": "decimate_collapse_plateau",
  "plateau_face_count": 8008,
  "target_face_count": 2000
}
```

## 5. Phase DM2. Preprocess and Topology Diagnosis

### 목적

ZBrush Decimation Master의 Pre-process에 해당하는 단계다. decimation 전에 mesh의 위험 요소와 보존해야 할 제약을 구조화한다.

### 분석 항목

- connected components
- tiny detached components
- boundary edges
- non-manifold edges
- degenerate faces
- duplicate vertices / near duplicate vertices
- duplicate faces
- very small triangles
- face area distribution
- material boundary
- UV seam boundary
- sharp normal boundary

### 출력

`decimation_diagnosis.json`

```json
{
  "component_count": 25,
  "largest_component_face_ratio": 0.98,
  "boundary_edge_count": 954,
  "non_manifold_edge_count": 1747,
  "tiny_component_count": 20,
  "recommended_policy": "component_budget"
}
```

### 완료 기준

- Blender 없이 가능한 부분은 pure geometry test로 검증
- Blender mesh에서 anchor lowpoly 진단 수치가 report로 남음
- diagnosis 결과가 retry policy 선택에 사용됨

## 6. Phase DM3. Component Budget Policy

### 목적

아주 낮은 target face count에서는 작은 detached component들이 face budget을 과도하게 차지한다. ZBrush식 decimation에서도 중요하지 않은 작은 조각은 과감히 줄거나 사라질 수 있다.

### 정책

옵션:

- `--component-policy preserve_all`
- `--component-policy budget`
- `--component-policy largest_only`

`budget` 동작:

- component별 bbox size, surface area, face count, material importance 계산
- 전체 target face budget을 component importance에 따라 배분
- tiny component는 최소 triangle shell 또는 제거 후보로 표시
- 제거는 기본 off, `strict_target`에서만 허용

### 완료 기준

- `anchor.obj`에서 component별 face budget report 생성
- tiny component 제거 없이 도달 가능한 하한과 제거 허용 시 하한을 비교
- 제거된 component가 있으면 report에 object/face count를 명시

## 7. Phase DM4. Importance Map

### 목적

ZBrush식 decimation의 핵심은 모든 영역을 같은 비율로 줄이지 않는 것이다. hard edge, silhouette, curvature, seam 등은 높은 중요도를 갖고, flat 영역은 낮은 중요도를 갖는다.

### importance source

- curvature
- dihedral angle
- boundary
- non-manifold boundary
- material boundary
- UV seam
- sharp normal boundary
- face area percentile
- optional user vertex group

### 출력

vertex / edge / face importance를 0.0-1.0 범위로 계산한다.

```json
{
  "importance_stats": {
    "min": 0.0,
    "mean": 0.27,
    "max": 1.0
  },
  "sources": {
    "curvature": true,
    "hard_edge": true,
    "boundary": true,
    "material_boundary": true,
    "uv_seam": true
  }
}
```

### Blender modifier 연결

단기:

- vertex group weight로 Blender Decimate Collapse에 전달
- feature 보호 강도를 `preserve_features_strength`로 조절

중기:

- custom QEM collapse의 edge collapse cost에 importance penalty로 사용

## 8. Phase DM5. Progressive Decimation Retry

### 목적

Collapse가 plateau에 걸렸을 때 voxel/cluster로 바로 넘어가지 않고, 같은 triangle decimation 계열 안에서 추가 시도를 한다.

### retry ladder

```text
Attempt 1: Collapse + full feature protection
Attempt 2: Collapse + relaxed feature protection
Attempt 3: cleanup constraints + Collapse
Attempt 4: flat-region planar reduction + triangulate + Collapse
Attempt 5: component budget policy + Collapse
Attempt 6: custom QEM triangle collapse
```

### 각 attempt report

```json
{
  "attempt": 4,
  "method": "planar_flat_region_reduce_then_collapse",
  "input_faces": 8008,
  "actual_faces": 4312,
  "shape_status": "accepted",
  "target_band": "failed"
}
```

### 완료 기준

- target을 못 맞춘 이유가 attempt별로 설명됨
- shape accepted를 유지하는 한 더 공격적인 시도를 자동 진행
- shape failed가 되면 이전 accepted attempt로 rollback

## 9. Phase DM6. Custom QEM Triangle Collapse

### 목적

Blender Decimate modifier가 topology plateau에 걸리는 한계를 넘기 위해 자체 triangle simplifier를 도입한다.

### 알고리즘

Quadric Error Metrics 기반 edge collapse:

- 각 vertex에 incident face plane quadric 누적
- collapse candidate edge 생성
- collapse error 계산
- feature/importance penalty 추가
- boundary/seam collapse 제약 적용
- priority queue로 lowest-cost edge부터 collapse
- degenerate face 제거
- target face count까지 반복

### collapse cost

```text
cost =
  qem_error
  + feature_penalty
  + boundary_penalty
  + normal_flip_penalty
  + uv_seam_penalty
  + component_policy_penalty
```

### 구현 위치

- pure geometry core: `retopo_agent/geometry/qem_decimate.py`
- Blender adapter: `retopo_agent/blender/qem_decimate.py`
- tests: `tests/test_retopo_qem_decimate.py`

### 성능 전략

Python만으로 9.8M face 전체 QEM은 느릴 수 있다.

단계적 접근:

1. Blender Collapse로 먼저 수천-수만 face까지 줄임
2. plateau 이후 남은 `8008 -> 2000` 구간에 custom QEM 적용
3. 추후 NumPy heap 최적화 또는 C++/Rust extension 검토

### 완료 기준

- synthetic mesh에서 target error <= 15%
- anchor plateau result `8008 -> 2000` 후처리 성공
- shape status가 accepted 또는 retry
- n-gon 없음

## 10. Phase DM7. Shape-Aware Rollback

### 목적

target을 맞추기 위해 형상을 망가뜨리면 ZBrush식 decimation 목표에 어긋난다. 각 aggressive attempt 후 shape 평가를 수행하고, 실패하면 이전 결과로 되돌린다.

### 정책

- target accepted + shape accepted: 최종 성공
- target accepted + shape retry: warning success
- target failed + shape accepted: best effort, 다음 attempt 진행
- shape failed: rollback to previous accepted shape result

### report

`decimation_attempts.json`

```json
{
  "selected_attempt": 6,
  "selection_reason": "target accepted and shape accepted",
  "attempts": []
}
```

## 11. Phase DM8. User-Facing Policies

### 목적

사용자가 원하는 결과는 상황마다 다르다. 목표 face count 우선인지, 형태 보존 우선인지 모드를 분리한다.

### 정책

```text
strict_shape
  target보다 shape 보존 우선
  component 제거 없음
  feature 보호 강함

balanced
  기본값
  accepted/retry band 안에서 target과 shape 균형
  tiny component budget 조정 허용

strict_target
  target face count 우선
  tiny component 제거 허용
  feature 보호 완화 허용
  최후 fallback으로 voxel/cluster 허용
```

### CLI

```bash
--decimation-policy balanced
--component-policy budget
--allow-component-removal false
--allow-remesh-fallback false
```

## 12. Phase DM9. Anchor Regression Suite

### 목적

`sample/anchor.obj`는 실제 실패를 드러낸 중요한 regression case다. 무거운 테스트이므로 일반 unit test와 분리한다.

### 구성

- `.context` 또는 `scripts`에 manual benchmark runner 유지
- pytest marker: `slow_blender`
- CI 기본 제외
- 로컬 수동 실행

### 테스트 항목

- Collapse plateau 감지
- diagnosis report 생성
- `balanced` policy 결과
- `strict_target` policy 결과
- shape report
- normal cleanup report
- export 성공

### 성공 기준

```text
balanced:
  target band: retry 또는 accepted
  shape: accepted

strict_target:
  actual faces: 1700-2300
  n-gon: 0
  shape: accepted 또는 retry
```

## 13. 구현 우선순위

1. DM1 Plateau Detection and Reporting
2. DM2 Preprocess and Topology Diagnosis
3. DM3 Component Budget Policy
4. DM5 Progressive Decimation Retry
5. DM4 Importance Map 고도화
6. DM6 Custom QEM Triangle Collapse
7. DM7 Shape-Aware Rollback
8. DM8 User-Facing Policies
9. DM9 Anchor Regression Suite

우선 DM1-D3를 먼저 구현하면 현재 anchor 실패의 원인을 제품 리포트에서 설명할 수 있고, DM5-DM6를 통해 2000 face 목표 달성 가능성을 높일 수 있다.

## 14. 권장 단기 마일스톤

### Milestone A. Explainable Failure

목표: 실패를 정확히 설명한다.

- plateau detection
- diagnosis report
- component report
- attempt history report

완료 기준:

`anchor.obj -> 2000` 결과가 `failed`여도 “왜 8008에서 멈췄는지” JSON과 콘솔 로그에 명확히 남는다.

### Milestone B. Better Blender-Only Decimation

목표: Blender modifier와 cleanup 조합만으로 가능한 한 더 낮춘다.

- relaxed feature protection
- limited dissolve / planar flat cleanup
- component budget
- shape-aware rollback

완료 기준:

`anchor.obj -> 2000`에서 8008보다 낮은 face count를 만들고 shape accepted/retry를 유지한다.

### Milestone C. QEM Post-Plateau Simplifier

목표: Blender Collapse plateau 이후 custom QEM으로 target을 맞춘다.

- `8008 -> 2000` custom simplification
- importance penalty
- boundary/seam constraints
- shape-aware rollback

완료 기준:

`strict_target`에서 `anchor.obj -> 2000`을 15% 이내로 달성한다.

## 15. 판단 기준

이 계획이 ZBrush Decimation Master에 더 가까운 이유:

- 단순 modifier ratio가 아니라 pre-process 기반으로 decimation을 수행한다.
- 모든 영역을 균등하게 줄이지 않고 importance map을 사용한다.
- flat 영역을 더 공격적으로 줄이고 feature 영역을 보호한다.
- target miss를 remesh로 바로 덮지 않고 decimation 계열 retry를 수행한다.
- 최종 결과를 face count만이 아니라 shape / normal / topology report로 판정한다.

다만 ZBrush 내부 알고리즘을 동일하게 복제하는 것은 아니다. Blender 환경에서 재현 가능한 범위 안에서 ZBrush식 동작 원리를 따라가는 계획이다.
