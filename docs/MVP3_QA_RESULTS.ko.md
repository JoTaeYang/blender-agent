# Electron UV Review App MVP 3 — QA Results

> 대상 계획: `docs/ELECTRON_UV_REVIEW_APP_MVP3_PRODUCTION_PLAN.ko.md`
> 범위: MVP 3 Generate + Optimize 파이프라인 검증 (Session A~G)
> 작성: 구현 세션 통합 검증

---

## 1. 검증 환경

| 항목 | 값 |
| --- | --- |
| Blender | 5.0.1 |
| Python | 3.14.2 / pytest 9.0.3 |
| Node | electron-vite 5.4.21 / tsx 테스트 러너 |
| 샘플 | `sample/SM_Test_Pottery_a_02.fbx` (UVChannel_1, 12,152 faces / 18,701 edges / 6,562 verts) |
| user seam spec | MVP 2 reference-boundary 추출(`UVChannel_1`) → `user_seam_edges=724`, `protected=0` |

> MVP 3는 MVP 2의 `active_user_seam_spec`을 **source of truth**로 사용한다. worker는
> 엄격한 user/reference 모드(`auto_refine/repair/enforce_mandatory/gate_mandatory = false`)로
> unwrap/pack/optimize만 수행하며, seam set을 자동으로 바꾸지 않고 source working model이나
> user seam spec을 덮어쓰지 않는다(계획 §1, §6, §14).

---

## 2. 실행 커맨드 (실제 Blender)

### 2.1 reference boundary spec 준비 (MVP 2 재사용)

```bash
blender --background --python worker/seam_editor_worker.py -- --job seam_job.json
# command=extract_uv_boundary_as_seams, uv_layer=UVChannel_1
```

결과: `status=accepted`, `user_seam_count=724`. 생성 spec이 `UserSeamSpec.from_dict()`로 load됨.

### 2.2 generate_uv_from_seams (계획 §4.1)

```bash
blender --background --python worker/generate_uv_from_seams.py -- --job gen_job.json
# job: {"command":"generate_uv_from_seams","model":".../SM_Test_Pottery_a_02.fbx",
#       "object_name":"SM_Test_Pottery_a_02","seam_spec":".../reference_boundary_seam_spec.json",
#       "out_dir":"runs/uv_run_qa",
#       "selected_blend_out":".../work/uv/selected_uv.blend",
#       "selected_summary_out":".../work/uv/selected_uv_summary.json"}
# options 기본값(strict) + layout_opt_max_candidates=24
```

실행 시간: **약 57s** (24 candidate sweep + baseline/selected preview 6장 + selected blend 저장). exit 0.

---

## 3. generate_uv_from_seams 결과 (accepted)

`uv_generate_summary.json` 핵심 필드:

| 필드 | 값 |
| --- | --- |
| `status` | `accepted` |
| `selected_candidate_id` | `slim_concave_m002` |
| `selected_uv_model` | `work/uv/selected_uv.blend` |
| `metrics.stretch_score` | 0.06866 |
| `metrics.worst_island_distortion` | 0.202999 |
| `metrics.raster_overlap_ratio` | 0.0 |
| `metrics.overlap_ratio` | 0.0 |
| `metrics.texel_density_variance` | 2e-06 |
| `metrics.packing_efficiency` | 0.591278 |
| `metrics.island_count` | 52 |
| `metrics.uv_bounds_ok` | true |
| `warnings` | `[]` |

> 실측치가 계획 §4.1 예시 출력과 동일하다(예시가 동일 pottery 실측에서 유도된 것으로 보임).

### 3.1 Seam integrity (계획 §6 — MVP 3 hard acceptance)

| 필드 | 값 | 판정 |
| --- | --- | --- |
| `user_seam_count` | 724 | — |
| `final_seam_count` | 724 | `== user_seam_count` ✅ |
| `auto_added_seams` | 0 | `== 0` ✅ |
| `user_protected_count` | 0 | — |
| `mandatory_rule_enabled` | false | report-only ✅ |
| `mandatory_gate_enabled` | false | report-only ✅ |
| `valid` | **true** | ✅ |

seam set이 전혀 바뀌지 않았다(`724 → 724`, auto-added 0). **PASS**

### 3.2 Layout optimization (계획 §5)

| 필드 | 값 |
| --- | --- |
| `enabled` | true |
| `selected_candidate_id` | `slim_concave_m002` |
| `kept_baseline` | false |
| `candidate_count` | 24 (= cap) |
| `score_before → after` | 0.008972 → -0.003276 |
| `packing_efficiency_before → after` | 0.583109 → 0.591278 |
| `stretch_before → after` | 0.06866 → 0.06866 |

`candidate_summary.json`: `baseline_candidate_id=slim_concave_m005`, `selected=slim_concave_m002`,
`candidates=24`, `rejected=0`. selected id가 summary / candidate_summary / layout block에서 일치. **PASS**

---

## 4. Artifact 검증 (계획 §7)

run folder (`runs/uv_run_qa/`)에 생성된 산출물:

```
baseline_uv_layout.png      baseline_checker_front.png   baseline_checker_side.png
selected_uv_layout.png      selected_checker_front.png   selected_checker_side.png
uv_generate_summary.json    candidate_summary.json       p5_gate.json   seam_report.json
selected_uv.blend           status.json   stdout.log     stderr.log
```

필수 6장 preview 모두 존재. **PASS**

`work/uv/` handoff(accepted run만, 계획 §6/§9):

```
work/uv/selected_uv.blend            (selected layout, 약 540 KB)
work/uv/selected_uv_summary.json     (= summary + source_run_id)
```

### 4.1 selected_uv.blend 재오픈 (계획 §7, Session D acceptance)

```bash
blender --background --python verify_blend.py -- work/uv/selected_uv.blend
# VERIFY object=SM_Test_Pottery_a_02 uv_layers=['UVChannel_1','AI_UV'] materials=['default']
# VERIFY has_uv=True checker_persisted=False
```

- `.blend`가 다시 열리고 generated UV layer(`AI_UV`)를 포함 ✅
- preview checker material이 저장본에 **남지 않음**(`checker_persisted=False`) — blend는 selected UV 결과만 담음(계획 §7) ✅
- baseline preview 재unwrap이 selected blend 저장 **이후**에 일어나 selected 좌표가 보존됨 ✅

**PASS**

---

## 5. 실패/예외 경로 (e2e smoke)

`tests/e2e/test_mvp3_uv_generate.py` (실제 Blender, 3건 모두 PASS):

| 테스트 | 시나리오 | 결과 |
| --- | --- | --- |
| `test_missing_seam_spec_smoke` | 존재하지 않는 spec path | `status=failed`, `error.code=seam_spec_missing` ✅ |
| `test_invalid_seam_spec_smoke` | 범위 밖 edge id(`999999999`) | `status=failed`, `error.code=invalid_seam_spec`, `details.invalid_edges=[999999999]`, `work/uv/selected_uv.blend` **미생성** ✅ |
| `test_reference_boundary_generate_smoke` | pottery reference boundary 전체 흐름 | `status=accepted`, seam integrity valid, candidate sweep + 6 preview, selected blend 재오픈 ✅ |

invalid spec일 때 selected output을 만들지 않는다(계획 §6, §13). **PASS**

---

## 6. 자동화 테스트 결과

| 스위트 | 커맨드 | 결과 |
| --- | --- | --- |
| Python 전체 | `pytest` | **639 passed, 1 skipped** |
| MVP 3 contract+integrity+candidates+artifacts | `pytest tests/test_uv_generate_*.py` | **40 passed** (14/14/6/6) |
| MVP 3 e2e (Blender) | `pytest tests/e2e/test_mvp3_uv_generate.py` | 3 passed |
| Electron typecheck | `npm run typecheck` | PASS (node + web) |
| Electron build | `npm run build` | PASS (main/preload/renderer 번들) |
| Electron 통합 | `npm run test:integration` | **8 passed** (MVP 3 2건 포함) |

> Blender가 없는 환경에서는 `tests/e2e/test_mvp3_uv_generate.py`가 자동 skip되어 `pytest`가 green을 유지한다(계획 §11 Session G).

---

## 7. Production Acceptance Checklist (계획 §13)

Functional:

- [x] MVP 2 project에서 `active_user_seam_spec`을 찾고 validate
- [x] Generate UV run 시작 / strict user/reference defaults 적용
- [x] baseline UV + layout optimization candidate list 생성
- [x] selected candidate id 표시 (`slim_concave_m002`)
- [x] before/after UV layout + checker preview 생성
- [x] selected UV가 `work/uv/selected_uv.blend`에 저장
- [x] project.json에 `latest_uv_generate_run_id` + selected UV paths 기록 (통합 테스트)

Seam integrity:

- [x] `auto_added_seams == 0`
- [x] `final_seam_count == user_seam_count` (724 == 724)
- [x] protected edge가 자동 seam으로 바뀌지 않음
- [x] mandatory 90 diagnostics가 gate를 fail시키지 않음 (report-only)
- [x] invalid edge ids → selected output 미생성

Layout quality:

- [x] selected candidate가 raster overlap hard reject 통과 (raster=0.0)
- [x] selected candidate가 UV bounds check 통과 (`uv_bounds_ok=true`)
- [x] baseline regression guard 통과 / baseline retained case 처리 (candidate test)
- [x] candidate_count가 옵션 cap(24) 이하

Robustness:

- [x] worker failure가 structured JSON status로 처리 (app crash 없음)
- [x] stdout/stderr log가 run folder에 저장
- [x] image artifact 실패는 warning으로 표시 (구현 + contract test)
- [x] source working model / user seam spec 미수정

Quality:

- [x] Python tests 통과 (639)
- [x] Electron typecheck 통과
- [x] Electron renderer build 통과
- [x] sample pottery generate/optimize smoke 문서화 (본 문서)

---

## 8. MVP 3 Done Definition (계획 §16) 대응

1–15 데모 단계가 실제 Blender 1-pass로 충족됨:
working model + active seam spec → validate → strict generate → baseline UV →
candidate sweep(24) → `slim_concave_m002` 선택 → candidate table / before-after layout /
checker front·side → `auto_added_seams==0`, `final_seam_count==user_seam_count` →
`selected_uv.blend` 재오픈 가능(`AI_UV` layer 포함).

MVP 4 AI Review 세션은 `p5_gate.json`, `seam_report.json`, `candidate_summary.json`,
UV layout/checker preview, `user_seam_spec.json`을 읽어 시작할 수 있다.
