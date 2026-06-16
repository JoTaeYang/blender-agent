# 사용자 지정 Seam 기반 UV Pipeline 작업 계획서

> 대상: 다음 구현 세션(Opus).
> 결정: 자동 seam/chapter 분리는 메인 기능에서 내린다.
> 목표: 우리가 잘하는 low-poly 변환, UV unwrap/pack, checker/overlap 검증에 집중하고, seam/chapter 결정은 사용자 입력을 우선한다.

## 1. 배경

현재까지 여러 자동 seam 접근을 실험했다.

```text
1. rule-based chart solver
   - 90도 이상 mandatory seam
   - smooth seam 최소화
   - distortion 기반 island split

2. important region policy
   - face/front protected region
   - post-split reject

3. region-aware face recovery
   - face/front/side/back-neck 구역화
   - repair reroute
```

결론:

```text
UV correctness는 어느 정도 달성했다.
하지만 seam/chapter 설계 품질은 사용자가 원하는 수준이 아니다.
특히 얼굴, 옷, 캐릭터/스태추 부위가 자연스럽게 나뉘지 않는다.
```

사용자 판단:

```text
지금 seam 나누는 품질이 좋지 않다.
부위별로 잘 쪼개지지도 않는다.
원하는 결과가 아니다.
```

따라서 제품 방향을 바꾼다.

## 2. 핵심 결정

자동 seam/chapter 생성은 메인 기능에서 내린다.

우리가 책임질 것:

```text
1. high poly -> low poly
2. 사용자가 지정한 seam/chapter를 기반으로 UV unwrap
3. UV packing
4. checker distortion 측정
5. overlap / raster overlap 검증
6. mandatory 90도 seam audit
7. texel density / island count / report
```

사용자에게 맡길 것:

```text
1. 중요한 seam 위치
2. 얼굴/옷/팔/손 같은 chapter 분리
3. protected / preferred seam 영역
4. 최종 seam 의도 판단
```

자동 seam은 삭제하지 않는다.
다만 다음 위치로 내린다.

```text
auto seam = experimental / suggestion / draft
user seam = authoritative source of truth
```

## 3. 제품 워크플로우

새 기본 워크플로우:

```text
1. 사용자가 high-poly object 입력
2. 앱이 low-poly 생성
3. 앱이 90도 이상 hard edge를 자동 표시
4. 사용자가 seam/chapter를 직접 지정하거나 수정
5. 앱이 사용자의 seam을 기준으로 UV unwrap
6. 앱이 UV pack
7. 앱이 checker distortion / overlap / 90도 누락을 검증
8. 앱이 문제 부위를 report
9. 사용자가 필요한 seam을 추가/삭제
10. 앱이 재 unwrap/pack
```

중요한 관점:

```text
사용자가 seam을 결정한다.
앱은 그것을 정확히 적용하고, 나쁜 결과를 정직하게 알려준다.
```

## 4. 이번 작업의 목표

이번 milestone의 목표:

```text
사용자 지정 seam을 입력받아 UV를 생성하는 안정적인 pipeline
```

구체적으로:

```text
1. user seam spec JSON 정의
2. Blender mesh의 seam edge id / selected edge / named edge group을 읽는 경로 정리
3. user seam을 authoritative seam으로 적용
4. mandatory 90도 edge와 user seam conflict를 report
5. user seam 기반 unwrap/pack 실행
6. checker distortion과 overlap 검증
7. 재실행 가능한 report 생성
```

## 5. 비목표

이번 작업에서 하지 말 것:

- 자동 face/chapter segmentation 품질 개선
- region-aware face recovery 추가 개선
- 새 UV 엔진 생성
- LLM이 edge-level seam을 직접 결정하게 만들기
- 자동 seam 결과를 최종 결과로 강제
- threshold를 완화해서 gate를 통과시키기

이번 작업은 자동 seam 개선이 아니라 **사용자 seam 기반 UV pipeline 안정화**다.

## 6. User Seam Spec

새 spec 파일 형식을 정의한다.

예:

```json
{
  "version": 1,
  "object": "humanstatue_low",
  "mode": "user_seams",
  "mandatory_fold_angle": 90.0,
  "user_seam_edges": [123, 456, 789],
  "user_protected_edges": [3054],
  "chapters": [
    {
      "name": "face",
      "face_ids": [1, 2, 3],
      "seam_edges": [123, 456],
      "protected_edges": [777]
    }
  ],
  "notes": "User-authored seam plan"
}
```

최소 필수:

```text
user_seam_edges
user_protected_edges
mandatory_fold_angle
```

`chapters`는 1차에서는 report용으로만 사용해도 된다.
즉, unwrap은 edge set 기준으로 수행하고, chapter는 UI/report grouping에 쓴다.

## 7. Seam Precedence

명확한 우선순위:

```text
1. mandatory 90도 edge
2. user_seam_edges
3. user_protected_edges
4. auto suggested seams
```

규칙:

```text
mandatory 90도 edge:
  항상 seam

user_seam_edges:
  seam으로 적용

user_protected_edges:
  seam 생성 금지
  단, mandatory 90도 edge와 충돌하면 mandatory wins

auto suggested seams:
  기본 off
  사용자가 요청할 때만 suggestion으로 제공
```

Conflict report:

```json
{
  "conflicts": [
    {
      "edge_id": 3054,
      "user_rule": "protected",
      "engine_rule": "mandatory_90",
      "resolution": "mandatory_wins"
    }
  ]
}
```

## 8. 구현 위치

### 8.1 새 모듈

```text
artist_uv_agent/user_seams.py
```

역할:

```text
UserSeamSpec load/save
edge id validation
conflict resolution
final seam set assembly
chapter report helper
```

예상 API:

```python
@dataclass
class UserSeamSpec:
    version: int
    object: str
    mandatory_fold_angle: float
    user_seam_edges: set[int]
    user_protected_edges: set[int]
    chapters: list[UserChapter]

def load_user_seam_spec(path: str) -> UserSeamSpec:
    ...

def build_user_seam_set(mesh, spec) -> UserSeamResult:
    ...
```

### 8.2 `chart_uv_agent.pipeline.run_chart_uv`

추가 parameter:

```python
def run_chart_uv(..., user_seam_spec=None, auto_refine=False, ...):
    ...
```

권장 동작:

```text
user_seam_spec 있음:
  initial seams = mandatory_90 + user_seam_edges
  user_protected_edges는 forbidden으로 전달
  auto distortion split은 기본 off 또는 report-only
  unwrap/pack/evaluate 실행

user_seam_spec 없음:
  기존 chart solver 동작 유지
```

주의:

```text
user seam mode에서 앱이 마음대로 seam을 추가하지 않는다.
필요한 seam이 부족하면 report로 알려준다.
```

### 8.3 Worker

`worker/run_quad_retopo_job.py`에 옵션 추가:

```text
--user-seam-spec path/to/user_seams.json
--auto-refine-user-seams false
```

동작:

```text
--user-seam-spec 있음:
  chart P5가 user seam mode로 실행

--user-seam-spec 없음:
  기존 chart P5 유지
```

## 9. Report

`seam_report.json`에 user seam block을 추가한다.

예:

```json
{
  "mode": "user_seams",
  "user_seams": {
    "user_seam_count": 120,
    "user_protected_count": 14,
    "mandatory_90_edges": 83,
    "final_seam_count": 203,
    "auto_added_seams": 0,
    "conflicts": [
      {
        "edge_id": 3054,
        "user_rule": "protected",
        "engine_rule": "mandatory_90",
        "resolution": "mandatory_wins"
      }
    ],
    "invalid_edges": []
  }
}
```

추가 검증 report:

```text
mandatory_90_missing
mandatory_90_uv_unsplit
overlap_ratio
raster_overlap_ratio
stretch_score
worst_island_distortion
island_count
texel_density_variance
uv_bounds_ok
```

## 10. Quality Feedback

사용자 seam을 그대로 적용한 뒤, 앱은 문제를 알려준다.

예:

```text
이 island는 checker distortion이 높음
이 영역은 overlap 있음
이 90도 edge는 seam이어야 함
이 protected edge는 mandatory와 충돌함
이 island가 너무 작음
```

중요:

```text
앱이 자동으로 고치지 말고, 먼저 report한다.
```

옵션으로만:

```text
auto_refine_user_seams=true
```

일 때만 distortion split을 추가한다.
이 경우에도 추가 seam은 `auto_added_seams`로 분리해서 report한다.

## 11. 테스트 계획

### 11.1 Pure tests

새 테스트:

```text
tests/test_user_seams.py
```

테스트 항목:

```text
UserSeamSpec JSON load/save
invalid edge id 검출
mandatory 90도 edge가 user_protected와 충돌하면 mandatory wins
user_seam_edges가 final seam set에 포함됨
user_protected_edges는 non-mandatory인 경우 final seam set에서 제외됨
chapters가 report에 포함됨
```

### 11.2 Blender run

테스트 spec 생성:

```text
.context/user_seam_specs/humanstatue_manual_seed.json
```

실행 예:

```bash
/Applications/Blender.app/Contents/MacOS/Blender --background \
  --python worker/run_quad_retopo_job.py -- \
  --p5-resume true \
  --uv-engine chart \
  --target-faces 5850 \
  --mesh-blend out/_face_recovery_snapshot/adaptive_t5850.blend \
  --reference .context/attachments/8Btvr3/humanstatue_low.obj \
  --user-seam-spec .context/user_seam_specs/humanstatue_manual_seed.json \
  --out-dir .context/runs/user_seam_uv_pipeline
```

검증:

```bash
uv run python - <<'PY'
import json, pathlib
d = pathlib.Path(".context/runs/user_seam_uv_pipeline")
gate = json.loads((d / "p5_gate.json").read_text())
report = json.loads((d / "seam_report.json").read_text())
print(gate["mandatory_90_missing"], gate["mandatory_90_uv_unsplit"])
print(report.get("mode"))
print(report.get("user_seams"))
PY
```

## 12. 성공 기준

```text
user_seam_spec 없이 실행하면 기존 chart solver 결과가 유지된다.
user_seam_spec이 있으면 user_seam_edges가 final seam set에 반영된다.
non-mandatory user_protected_edges는 final seam set에서 제외된다.
mandatory/protected conflict는 report된다.
auto_added_seams는 기본 0이다.
mandatory_90_missing == 0
mandatory_90_uv_unsplit == 0
seam_report.json이 user seam mode를 설명한다.
```

시각 품질 기준:

```text
사용자가 지정한 seam/chapter가 UV 결과에 그대로 반영되어야 한다.
앱이 임의로 얼굴/옷 seam을 다시 나누지 않아야 한다.
```

## 13. 실패 기준

```text
사용자 seam을 무시함
사용자 protected edge를 non-mandatory인데도 seam으로 유지함
auto seam이 기본으로 추가됨
mandatory 90도 seam이 누락됨
report 없이 conflict를 숨김
기존 no-spec chart path가 바뀜
```

## 14. Opus에게 주는 핵심 지시

이번 작업은 자동 seam 품질 개선이 아니다.

해야 할 일:

```text
사용자 seam spec을 source of truth로 삼는 UV pipeline
user seam 기반 unwrap/pack/evaluate
quality feedback report
conflict report
no-spec path regression 방지
```

하지 말 것:

```text
자동 face/chapter 분리 개선
region-aware face recovery 추가 개선
새 engine 만들기
auto seam을 기본으로 추가하기
threshold 완화로 gate 통과시키기
```

## 15. 기대 결과

이 방향으로 가면 제품 책임 범위가 명확해진다.

```text
우리가 잘하는 것:
  low-poly 변환
  UV unwrap
  UV packing
  distortion/overlap 검증
  mandatory seam audit

사용자가 결정하는 것:
  중요한 seam 위치
  얼굴/옷/팔/손 chapter
  최종 seam 의도
```

이게 현재 품질과 일정 기준에서 가장 현실적인 방향이다.
