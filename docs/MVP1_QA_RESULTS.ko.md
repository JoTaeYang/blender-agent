# Electron UV Review App MVP 1 — QA Results

> 대상 계획: `docs/ELECTRON_UV_REVIEW_APP_MVP1_PRODUCTION_PLAN.ko.md`
> 범위: MVP 1 read-only UV review 파이프라인 검증 (Session A~F)
> 작성: 구현 세션 통합 검증

---

## 1. 검증 환경

| 항목 | 값 |
| --- | --- |
| Blender | 5.0.1 (hash a3db93c5b259) |
| Python | 3.14.2 / NumPy 2.4.2 / pytest 9.0.3 |
| Node | v24.8.0 / npm 11.6.0 |
| 샘플 | `sample/SM_Test_Pottery_a_02.fbx` (UVChannel_1, 12,152 faces) |
| no-UV fixture | 즉석 생성 (vt 없는 cube OBJ; `*.obj`는 git ignore이므로 커밋하지 않음) |

> 중요: Blender 5.x의 `bpy.ops.uv.export_layout` PNG 모드는 GPU 드로잉을 요구하여
> `blender --background`에서 동작하지 않는다("GPU functions for drawing are not
> available in background mode"). 따라서 `uv_layout.png`는 NumPy 래스터라이저
> (`uv_agent/geometry/uv_review.py` + `uv_agent/io/png.py`)로 headless 생성한다.
> EEVEE 체커 렌더는 background에서 정상 동작한다. (계획 §7, §13의 fallback 정책 준수)

---

## 2. 실행 커맨드

### inspect_uv_layers

```bash
blender --background --python worker/review_existing_uv.py -- --job inspect_job.json
# job: {"command":"inspect_uv_layers","model":".../SM_Test_Pottery_a_02.fbx",
#       "model_rel":"sample/SM_Test_Pottery_a_02.fbx","out":"inspect_result.json"}
```

결과: `status=accepted`, object `SM_Test_Pottery_a_02`, faces 12152,
uv_layers `[UVChannel_1]`, active `UVChannel_1`, has_uv `true`,
`recommended_next_step=review_existing_uv`. **PASS**

### review_existing_uv (UV 있음)

```bash
blender --background --python worker/review_existing_uv.py -- --job runs/review_1/job.json
# job: {"command":"review_existing_uv","object_name":"SM_Test_Pottery_a_02",
#       "uv_layer":"UVChannel_1","options":{"texture_size_px":1024,"render_size_px":700,
#       "raster_overlap_resolution":1024},"out_dir":"runs/review_1"}
```

소요: ~1.4s. `status.json=accepted`. **PASS**

### review_existing_uv (no-UV)

```bash
blender --background --python worker/review_existing_uv.py -- --job runs/review_realnouv/job.json
# model = vt 없는 cube OBJ
```

결과: `status.json=no_uv`, summary `metrics=null`, `uv_layer=null`,
이미지 artifact 미생성. **PASS**

---

## 3. 메트릭 결과 (pottery `UVChannel_1`)

mesh: vertices 6562 / edges 18701 / faces 12152 / loops 36896
review_status: `has_overlap` (issues: raster_overlap, overlap, out_of_bounds, high_stretch, density_variance)

| metric | value |
| --- | --- |
| stretch_score | 2.135549 |
| worst_island_distortion | 3.283340 |
| overlap_ratio | 0.871945 |
| raster_overlap_ratio | 0.025878 |
| self_overlap_ratio | 0.001253 |
| cross_overlap_ratio | 0.024625 |
| texel_density_variance | 1.856265 |
| packing_efficiency | 0.329755 |
| island_count | 52 |
| uv_bounds | min [-1.470, -6.496] / max [2.195, 6.504] / in_0_1 `false` |

> 해석: 이 샘플의 아티스트 UV(UVChannel_1)는 [0,1] 타일을 벗어나 여러 UDIM 타일에
> 걸쳐 있는 레이아웃이라 `out_of_bounds`와 overlap이 보고된다. MVP 1은 read-only
> review이므로 이 결과는 **수정 없이** 그대로 표시한다(계획 §6, §13 "reviewer aid").

---

## 4. Artifact 검증

| artifact | 생성 | 크기 |
| --- | --- | --- |
| uv_review_summary.json | ✅ | 2.2 KB |
| uv_metrics.json | ✅ | 11.6 KB |
| uv_layers.json | ✅ | — |
| uv_bounds.json | ✅ | — |
| uv_layout.png | ✅ | 16 KB (headless 래스터, 시각 확인됨) |
| uv_layout.svg | ✅ | 1.6 MB (vector fallback) |
| checker_front.png | ✅ | 174 KB (EEVEE 체커, UV 왜곡 가시) |
| checker_side.png | ✅ | 181 KB |

summary의 artifact 경로는 run-relative 파일명으로 기록(`uv_layout.png` 등).
Electron main이 절대경로로 변환하여 `uvpreview://`로 렌더에 노출. **PASS**

---

## 5. 자동화 테스트 결과

| 테스트 | 결과 |
| --- | --- |
| `tests/test_uv_review_contract.py` | PASS (11) |
| `tests/test_uv_review_metrics.py` | PASS (10) |
| `tests/test_uv_review_artifacts.py` | PASS (6, Blender 렌더 smoke 포함) |
| `tests/e2e/test_mvp1_uv_review.py` | PASS (3, 실제 Blender) |
| 전체 `pytest` | PASS (회귀 없음) |
| `npm run typecheck` (node+web) | PASS |
| `npm run build` | PASS |
| `npm run test:integration` (mock worker) | PASS (4) |

> Blender 미설치 환경에서는 Blender-gated 테스트가 skip 되어 `pytest`가 green을
> 유지한다(계획 §12 정책).

---

## 6. Production Acceptance Checklist (계획 §12)

Functional

- [x] MVP 0 `project.json`의 working_model 해석 (working_model → fbx → source fallback)
- [x] direct FBX/OBJ/GLB/GLTF + `.blend` import/open
- [x] object list + UV layer list 표시 (inspect_uv_layers)
- [x] active UV layer 선택 + project state 저장 (set_active_uv_layer)
- [x] 기존 UV 기준 `uv_layout.png` 생성 (headless 래스터)
- [x] checker front/side 생성
- [x] stretch/overlap/density/packing 메트릭 UI 노출
- [x] no-UV → `status: no_uv`
- [x] mandatory 90 rule 미포함 (계산/gate 안 함)

Robustness

- [x] Blender 경로 미설정 시 mock fallback / setup error
- [x] 잘못된 UV layer name → structured error (`uv_layer_not_found`)
- [x] worker 실패가 앱 crash로 이어지지 않음 (structured status)
- [x] image artifact 실패는 warnings로 표시 (summary.warnings)
- [x] stdout/stderr log를 run folder에 저장
- [x] 원본 working model 미변경 (worker는 save 안 함, 임시 process에서만 material 적용)

Contract

- [x] 모든 command JSON in/out
- [x] 모든 review run에 `status.json`
- [x] accepted run에 `uv_review_summary.json`
- [x] Renderer는 stdout parse 안 함 (normalized JSON + artifact path만)
- [x] artifact path는 project-relative로 summary 저장

Quality

- [x] Python tests PASS
- [x] Electron typecheck PASS
- [x] Electron renderer build PASS
- [x] sample pottery UV review smoke 문서화 (본 문서)

---

## 7. Done Definition (계획 §15) 데모 대응

| 단계 | 상태 |
| --- | --- |
| 1. MVP 0 project open | ✅ (main `project:open`) |
| 2. project.json → working model | ✅ (resolveWorkingModel) |
| 3. UV Review workspace | ✅ (기본 화면) |
| 4. mesh object + UV layers 표시 | ✅ |
| 5. UVChannel_1 선택 | ✅ |
| 6. Review UV 실행 | ✅ (~1.4s) |
| 7. uv_layout.png 표시 | ✅ |
| 8. checker_front/side 표시 | ✅ |
| 9. stretch/overlap/density/packing/island/bounds 표시 | ✅ |
| 10. project.json에 latest_uv_review_run_id 기록 | ✅ (registerReviewRun) |
| 11. no-UV fixture → no-UV 상태 표시 | ✅ |

---

## 8. 알려진 한계 / 후속

- `uv.export_layout` PNG는 headless 불가 → NumPy 래스터로 대체(품질 충분, 메트릭과
  동일한 island 복원 사용). 필요 시 MVP 1.1에서 GPU offscreen 경로 추가 검토.
- `raster_overlap_*`는 [0,1] 타일 영역만 샘플링한다. UDIM/타일 밖 레이아웃의 overlap은
  signed `overlap_ratio`(전역)로 잡히지만 raster 수치는 타일 내부 기준이다.
- heatmap(`stretch_heatmap.png`) / `overlap_mask.png` / `checker_3q.png`는 optional로
  남김(계획 §7). `make_3q` 옵션으로 3/4 뷰 활성화 가능.
- Electron UI는 typecheck/build/main 통합테스트로 검증. 실제 창 렌더 수동 확인은
  데모 환경에서 별도 수행 권장.
