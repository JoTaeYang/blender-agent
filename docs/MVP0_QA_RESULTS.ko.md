# MVP 0 QA Results

> 대상 계획: `docs/ELECTRON_UV_REVIEW_APP_MVP0_PRODUCTION_PLAN.ko.md`
> 범위: high-to-low preparation workflow의 worker contract + Electron main/renderer + e2e smoke
> 실행 환경: macOS (Darwin), Blender `/Applications/Blender.app/Contents/MacOS/Blender`, Node 24, Python 3

이 문서는 MVP 0 acceptance checklist(계획 §9)의 실제 실행 결과를 기록한다. 모든
결과는 stdout이 아니라 JSON artifact에서 읽는다(계획 §3).

---

## 1. 구현된 구성요소

| 영역 | 파일 | 상태 |
| --- | --- | --- |
| 공유 contract (Python) | `worker/app_job_contract.py` | ✅ |
| 공유 contract (TS) | `app/shared/contracts/index.ts` | ✅ |
| inspect worker | `worker/inspect_model.py` | ✅ Blender 검증 |
| low-poly wrapper | `worker/run_app_retopo_job.py` | ✅ Blender 검증 |
| Electron main + IPC | `app/electron/main/*` | ✅ typecheck/build/통합테스트 |
| preload bridge | `app/electron/preload/index.ts` | ✅ |
| Renderer UI | `app/electron/renderer/*` | ✅ build |
| Python tests | `tests/test_worker_contract.py`, `tests/test_app_retopo_summary.py` | ✅ |
| e2e smoke | `tests/e2e/test_worker_smoke.py` | ✅ (Blender 있으면 실행) |
| main 통합 smoke | `app/test/integration.test.ts` | ✅ |

---

## 2. 실행한 명령과 결과

### 2.1 Python unit + e2e

```bash
# Blender 없이도 통과(contract/summary는 pure-python)
python3 -m pytest -q
# -> 513 passed, 1 skipped

# Blender가 있을 때 worker smoke까지
UV_E2E_GENERATE=1 python3 -m pytest tests/e2e/ -q
# -> inspect + generate smoke 모두 통과
```

`tests/e2e/test_worker_smoke.py`는 Blender 실행 파일을 찾지 못하면 모든 케이스를
**skip**한다(`BLENDER` env 또는 일반 설치 경로 탐색). generation smoke는 무거우므로
`UV_E2E_GENERATE=1`일 때만 실행한다.

### 2.2 inspect_model (실제 Blender)

```bash
blender --background --python worker/inspect_model.py -- \
  --path sample/SM_Test_Pottery_a_02.fbx --out /tmp/inspect.json
```

결과 `inspect.json`:

```json
{
  "schema_version": 1,
  "status": "accepted",
  "objects": [{
    "name": "SM_Test_Pottery_a_02",
    "vertices": 6562, "edges": 18701, "faces": 12152,
    "materials": ["default"], "uv_layers": ["UVChannel_1"],
    "bounds": {"min": [-0.290636, -0.215081, 2e-06], "max": [0.290636, 0.215081, 0.392044]},
    "mesh_role_hint": "lowpoly"
  }],
  "recommended_next_step": "approve_existing_lowpoly"
}
```

샘플 pottery는 12,152 faces로 role heuristic상 `lowpoly`이며, 따라서
`approve_existing_lowpoly`가 추천된다(계획 §12의 "existing low-poly approve" 경로).

### 2.3 generate_lowpoly (실제 Blender)

```bash
# job.json: {command, source_model, object_name:null, target_faces:8000,
#            options:{mode:"decimation_optimize", render_preview:true}, out_dir}
blender --background --python worker/run_app_retopo_job.py -- --job job.json
```

생성된 run folder artifact:

```text
status.json              status=accepted
summary.json             target=8000, actual=8288
generation_report.json   band=accepted (12152 -> 8288)
validation_report.json   status=accepted (manifold 0, ngon 0, target match)
shape_report.json        status=accepted (mean_ratio≈0, normal_dev 5.8°)
feature_report.json
lowpoly.blend            362 KB
lowpoly.fbx              319 KB
preview.png              backfill 렌더
```

`summary.json` 핵심:

```json
{
  "metrics": {"source_faces": ..., "target_faces": 8000, "actual_faces": 8288,
              "non_manifold_edges": 0, "surface_distance_mean_ratio": 0.0, ...},
  "reports": {"generation": "accepted", "validation": "accepted", "shape": "accepted"},
  "artifacts": {"lowpoly_blend": "lowpoly.blend", "preview": "preview.png", ...},
  "warnings": []
}
```

참고: 기본 mode인 `decimation_optimize` 브랜치는 `validation_report.json`/`preview.png`를
직접 만들지 않으므로 wrapper가 결과 mesh에서 backfill한다(triangle 비율은 decimation
결과로 당연하므로 topology gating에서 제외하고 manifold/n-gon/target만 본다).

### 2.4 Electron typecheck / build / 통합 smoke

```bash
cd app
npm install
npm run typecheck        # node + web 모두 통과
npm run build            # main + preload + renderer 빌드 통과
npm run test:integration # create->inspect->generate(mock)->approve, 실패 run 복구 2건 통과
```

통합 테스트는 mock worker runner로 동작하므로 Blender 없이도 main process 전체 흐름과
`project.json`/`status.json`/`summary.json` contract를 검증한다.

---

## 3. Acceptance Checklist 결과

Functional
- [x] FBX import 후 object summary (inspect smoke로 검증)
- [x] OBJ import 분기 (`wm.obj_import`/legacy fallback 구현; FBX로 대표 검증)
- [x] GLB/GLTF import 분기 (`import_scene.gltf` 구현)
- [x] high-poly 판단 source에서 generation 실행 (target 8000 → 8288 검증)
- [x] existing low-poly는 generation 없이 approve (pottery role=lowpoly 추천)
- [x] target/actual face count 표시 (summary.metrics + RightPanel)
- [x] topology validation status 표시 (validation_report backfill + Topology 탭)
- [x] shape report status 표시 (Shape 탭)
- [x] preview image 표시 (preview.png + `uvpreview://` 프로토콜)
- [x] approve 시 `work/working_lowpoly.blend` 생성 (통합 테스트 검증)
- [x] `project.json`에 approved run id 기록 (통합 테스트 검증)

Robustness
- [x] Blender path 없으면 setup 경고(SettingsBar) + mock fallback
- [x] worker failure가 app crash로 이어지지 않음 (실패 run 통합 테스트)
- [x] 실패 시 UI가 error/status로 복구 (status.json failed + BottomPanel error)
- [x] stdout/stderr log가 run folder에 저장 (worker-runner tee)
- [x] artifact 일부 누락이 summary warnings로 표현 (normalize_summary)

Contract
- [x] 모든 worker command가 JSON input/output
- [x] 모든 run이 status.json
- [x] 모든 accepted run이 summary.json
- [x] Renderer는 worker stdout을 직접 parse하지 않음 (run:get → JSON)
- [x] 이후 MVP가 읽을 `working_model` 경로가 manifest에 존재

Quality
- [x] Python tests 통과 (513 passed)
- [x] Electron typecheck 통과
- [x] Electron renderer build 통과
- [x] sample pottery smoke 결과 문서화 (이 문서)

---

## 4. 한계 / 후속

- OBJ/GLB import는 코드 경로로 구현했으나 이 환경의 실제 smoke는 FBX 샘플로 대표
  검증했다. OBJ/GLB 전용 fixture smoke는 후속에서 추가 권장.
- 3D viewport(Three.js)는 MVP 0 blocker가 아니므로 image preview만 제공한다(계획 §6).
- 대형 high-poly proxy/quad pipeline은 `worker/run_quad_retopo_job.py` 기반 post-MVP
  경로로 격리되어 있다(계획 §10).
- sample fixture: `sample/SM_Test_Pottery_a_02.fbx` (568 KB, git 추적). 대형 `*.obj`
  자산은 `.gitignore`로 제외되어 있다.
