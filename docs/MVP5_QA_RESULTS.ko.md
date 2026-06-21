# Electron UV Review App MVP 5 — QA Results

> 대상 계획: `docs/ELECTRON_UV_REVIEW_APP_MVP5_PRODUCTION_PLAN.ko.md`
> 범위: MVP 5 Production Export / History / Rollback 파이프라인 검증 (Session A~G)
> 작성: 구현 세션 통합 검증

---

## 1. 검증 환경

| 항목 | 값 |
| --- | --- |
| Blender | 5.0.1 |
| Python | 3.14.2 / pytest 9.0.3 |
| Node | electron-vite 5.4.21 / tsx 테스트 러너 |
| 샘플 | `sample/SM_Test_Pottery_a_02.fbx` (UVChannel_1, 12,152 faces / 6,562 verts) |
| selected UV fixture | MVP 3 accepted handoff(`work/uv/selected_uv.blend`) 대체로 pottery FBX를 사용(이미 `UVChannel_1` 보유) |

> MVP 5는 MVP 4 AI Review를 **건너뛴다**. export readiness는 MVP 3 metrics와 export
> validation에만 의존하며(계획 §0, §15), AI Review skip은 정보성 warning일 뿐 export를
> 막지 않는다. worker는 source `selected_uv_model` / `working_model` / `user_seam_spec`을
> 절대 덮어쓰지 않고(계획 §11, §15), 모든 export option은 **duplicate object**에만 적용한다.

---

## 2. 구현 범위 (Session A~G)

| 세션 | 산출물 | 상태 |
| --- | --- | --- |
| A. Export Worker Contract | `worker/app_export_contract.py`, `tests/test_export_contract.py` | ✅ |
| B. Blender Export Formats | `uv_agent/blender/export.py`, `tests/test_export_formats.py` | ✅ |
| C. Export Validation | `uv_agent/blender/export_validation.py`, `tests/test_export_validation.py` | ✅ |
| B/C. Export Worker | `worker/export_production_asset.py` | ✅ |
| D. History + Rollback | `app/electron/main/project-service.ts` (MVP 5 섹션), `app/shared/contracts/export.ts` | ✅ |
| E. Main Export IPC | `app/electron/main/exportRunner.ts`, `ipc.ts`, `preload/index.ts` | ✅ |
| F. Renderer Export UI | `app/electron/renderer/src/export/ExportWorkspace.tsx`, `App.tsx`, `styles.css` | ✅ |
| G. QA Fixtures + E2E | `tests/e2e/test_mvp5_export.py`, `app/test/integration.test.ts`, 본 문서 | ✅ |

---

## 3. 실행 커맨드 (실제 Blender)

### 3.1 export_production_asset (계획 §5.1)

```bash
blender --background --python worker/export_production_asset.py -- --job export_job.json
# job: {"command":"export_production_asset",
#       "selected_uv_model":".../SM_Test_Pottery_a_02.fbx",
#       "object_name":"SM_Test_Pottery_a_02",
#       "formats":["obj","fbx","glb"],
#       "options":{"selected_uv_layer":"UVChannel_1"},
#       "out_dir":"exports/export_smoke",
#       "uv_generate_run_id":"uv_run_smoke",
#       "selected_uv_summary":".../selected_uv_summary.json"}
```

실행: exit 0, `status=accepted`. obj/fbx/glb export + 3개 reopen validation + UV layout/checker preview 3장.

### 3.2 check_export_readiness (계획 §4)

```bash
blender --background --python worker/export_production_asset.py -- --job readiness_job.json
# command=check_export_readiness, selected_uv_model + selected_uv_summary
```

---

## 4. export_production_asset 결과 (accepted)

`export_manifest.json` 핵심 필드:

| 필드 | 값 |
| --- | --- |
| `status` | `accepted` |
| `formats` | `["obj","fbx","glb"]` |
| `source.uv_generate_run_id` | `uv_run_smoke` (source UV run 연결) |
| `source.active_user_seam_spec` | `work/seams/user_seam_spec.json` |
| `source.candidate_summary` | `runs/uv_run_smoke/candidate_summary.json` |
| `source.ai_review_run_id` / `ai_review_skipped` | `null` / `true` |
| `metrics` | `stretch_score / worst_island_distortion / raster_overlap_ratio / texel_density_variance / packing_efficiency` |
| `files` | `obj/fbx/glb` + `uv_layout.png` + `checker_front.png` + `checker_side.png` |
| `options` | `apply_scale=true, include_materials=true, include_normals=true, copy_textures=false, triangulate=false` |

export된 파일 크기: `fbx≈451KB`, `glb≈304KB`, `obj≈1.0MB`.

---

## 5. validation_report.json — reopen validation (계획 §7)

각 format을 **fresh scene에 재import**하여 mesh/UV/카운트를 측정. overall `status=accepted`.

| format | reopen | has_uv | uv_layers | faces | verts | warnings |
| --- | --- | --- | --- | --- | --- | --- |
| obj | ✅ | ✅ | `["UVMap"]` | 12,152 | 6,562 | 1 (UV layer 이름 변경) |
| fbx | ✅ | ✅ | `["UVChannel_1"]` | 12,152 | 6,562 | 0 |
| glb | ✅ | ✅ | `["UVMap"]` | 12,592 | 7,333 | 2 (이름 변경 + face count 차이) |

검증된 tolerance 정책(계획 §7):

- **missing UV = hard failure** — 세 format 모두 `has_uv=true`로 통과.
- FBX는 `UVChannel_1` 이름을 보존, OBJ/GLB는 `UVMap`으로 rename → **warning**(실패 아님).
- GLB는 glTF가 삼각형 전용이라 face count가 12,152 → 12,592로 증가 → **warning**(실패 아님).
- object/material naming, vertex split(6,562 → 7,333)도 warning으로만 보고.

> 즉 “honest reporting” 규칙(계획 §10, §15)을 그대로 따른다: 차이를 숨기지 않고 warning으로
> 노출하되 UV 누락만 hard failure로 처리한다.

---

## 6. 자동화 테스트 결과

### 6.1 Python 단위 테스트 (Blender 불필요)

```bash
pytest tests/ --ignore=tests/e2e
# 665 passed (기존 632 + MVP 5 신규 33)
pytest tests/test_export_contract.py tests/test_export_formats.py tests/test_export_validation.py
# 33 passed
```

| 파일 | 테스트 수 | 커버리지 |
| --- | --- | --- |
| `test_export_contract.py` | 23 | readiness 도출, export status policy(accepted/partial/failed), manifest/source builder, validation report 분류, status/이벤트 lifecycle |
| `test_export_formats.py` | 4 | OBJ axis enum 매핑, format dispatch, 기본 axis |
| `test_export_validation.py` | 6 | UV layer 이름 tolerance, face/vertex drift, normals warning |

### 6.2 Electron 메인 프로세스 integration (mock, Blender 불필요)

```bash
cd app && npm run test:integration   # 11 passed (기존 8 + MVP 5 신규 3)
```

- `mvp5 export: readiness -> export(mock) -> manifest -> history -> rollback`
  readiness accepted → export accepted → manifest가 source UV run/seam spec/candidate/metrics/files 연결 →
  `latest_export_id` + `export_created` history → rollback targets(uv_run + export) → uv_run rollback이
  `work/uv` pointer 복원 + `rollback_performed` append + **newer export 보존**.
- `mvp5 export readiness: missing selected UV is needs_input` — selected UV 없으면
  `needs_input` + `missing_selected_uv_model`, AI skip은 blocker 아님.
- `mvp5 export: rollback to a prior export re-pins latest_export_id, keeps newer` —
  export rollback이 `latest_export_id`만 재지정하고 newer export folder는 삭제 안 함.

### 6.3 Blender e2e smoke

```bash
pytest tests/e2e/test_mvp5_export.py     # 3 passed (Blender 있을 때) / skipped (없을 때)
```

- `test_export_smoke` — obj/fbx/glb export + manifest + per-format reopen(UV 존재) + preview.
- `test_export_missing_model_smoke` — 없는 selected UV → `failed` / `missing_selected_uv_model`,
  manifest 미생성(all-fail은 manifest 없음, 계획 §5/§14).
- `test_readiness_smoke` — accepted summary → ready, 없는 model → `needs_input`.

> Blender 미설치 환경에서는 e2e가 전부 **skip**되어 CI가 green을 유지한다(계획 §12 Session G).

### 6.4 Electron typecheck / build

```bash
cd app
npm run typecheck    # node + web 모두 통과
npm run build        # main + preload + renderer 빌드 통과
```

---

## 7. Production Acceptance Checklist (계획 §14)

Functional:

- [x] MVP 3 selected UV가 없으면 export가 막힌다 (`needs_input` / `missing_selected_uv_model`).
- [x] AI Review skipped 상태가 blocker가 아니다 (warning + `checks.ai_review_skipped`).
- [x] FBX export 가능.
- [x] OBJ export 가능.
- [x] GLB(+GLTF) export 가능.
- [x] exported file reopen validation 실행.
- [x] UV layer가 export 결과에 남아 있다 (세 format 모두 `has_uv=true`).
- [x] `export_manifest.json` 생성.
- [x] project history에 `export_created` event 추가.
- [x] 이전 UV run / export로 rollback 가능 (`rollback_performed` event).

Robustness:

- [x] source working model 덮어쓰지 않음 (duplicate에만 옵션 적용).
- [x] selected UV blend를 export 중 오염시키지 않음 (worker는 blend를 save하지 않음).
- [x] 일부 format 실패가 app crash로 이어지지 않음 (`export_one`이 format별 structured error).
- [x] stdout/stderr log가 export folder에 저장됨.
- [x] old exports는 new export/rollback이 삭제하지 않음.

Contract:

- [x] 모든 export command가 JSON in/out.
- [x] 모든 export가 `status.json`.
- [x] accepted/partial export가 `export_manifest.json`.
- [x] `validation_report.json`이 format별 reopen 결과 포함.
- [x] `project.json.latest_export_id`가 accepted/partial export를 가리킴.

Quality:

- [x] Python tests 통과 (665 + e2e 3).
- [x] Electron typecheck 통과.
- [x] Electron renderer build 통과.
- [x] sample export smoke 결과 문서화 (본 문서 §4~§5).

---

## 8. MVP 5 Done Definition 검증 (계획 §17)

| # | 시나리오 | 검증 |
| --- | --- | --- |
| 1 | App이 MVP 3 project를 연다 | `project:open` 기존 동작 |
| 2 | `selected_uv_model` / `selected_uv_summary` 발견 | readiness가 두 pointer를 읽음 |
| 3 | AI Review skipped를 non-blocking으로 표시 | `checks.ai_review_skipped=true`, warning만 |
| 4 | Export workspace 진입 | App.tsx `Export` 탭 |
| 5 | export readiness check 통과 | `exportCheckReadiness` → ready |
| 6 | FBX/OBJ/GLB 선택 | format 체크박스 |
| 7 | worker가 요청 format export | `export_production_asset` (e2e accepted) |
| 8 | reopen + UV presence validation | `validation_report.json` (§5) |
| 9 | exported files + validation 표시 | center Files/Validation 탭 |
| 10 | manifest가 source UV run/seam spec/candidate/metrics/files 연결 | `export_manifest.json` (§4) |
| 11 | history에 `export_created` | integration test 통과 |
| 12 | 이전 UV run/export로 rollback(확인 후) | `window.confirm` → `exportRollback` |
| 13 | history에 `rollback_performed` | integration test 통과 |

→ 13단계 demo가 mock(앱 레벨) + 실제 Blender(export/validation 레벨)로 분할 검증되어 **MVP 5 완료**.

---

## 9. 알려진 한계 / 메모

- **rollback / history는 pure-Node**(`project-service.ts`)라 Blender e2e가 아닌
  `app/test/integration.test.ts`로 검증한다. Blender e2e는 export/validation/manifest(실제 mesh)
  절반을 담당한다.
- **GLB는 glTF 특성상 항상 triangulate**되어 face count가 증가한다(예: 12,152 → 12,592).
  계획 §7 tolerance에 따라 warning으로만 보고하고 hard failure로 두지 않는다.
- **OBJ/GLB UV layer 이름**(`UVMap`)은 format 한계로 원본(`UVChannel_1`)과 다르며 warning 처리.
  FBX는 이름을 보존한다.
- `copy_textures`는 best-effort(GLB는 자동 embed). 텍스처가 없는 샘플에서는 no-op.
- partial export(일부 format만 validate)는 contract/단위 테스트로 검증(`classify_export_status`);
  실제 한 format만 실패시키는 Blender 시나리오는 향후 fixture로 추가 가능.
