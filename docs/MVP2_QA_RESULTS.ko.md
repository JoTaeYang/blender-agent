# Electron UV Review App MVP 2 — QA Results

> 대상 계획: `docs/ELECTRON_UV_REVIEW_APP_MVP2_PRODUCTION_PLAN.ko.md`
> 범위: MVP 2 User Seam Spec Editor 파이프라인 검증 (Session A~G)
> 작성: 구현 세션 통합 검증

---

## 1. 검증 환경

| 항목 | 값 |
| --- | --- |
| Blender | 5.0.1 |
| Python | 3.14.2 / NumPy 2.4.6 / pytest 9.0.3 |
| Node | v24.8.0 / npm 11.6.0 |
| Three.js | 0.184.0 (renderer viewport) |
| 샘플 | `sample/SM_Test_Pottery_a_02.fbx` (UVChannel_1, 12,152 faces / 18,701 edges) |
| no-UV fixture | 즉석 생성 (vt 없는 cube OBJ; `*.obj`는 git ignore이므로 커밋하지 않음) |

> MVP 2는 **비생성(non-generative)** 단계다. worker는 UV를 unwrap/pack/수정하지 않고,
> mesh에 seam을 굽지 않으며, mandatory-90 fold를 자동 seam으로 추가하지 않는다(계획 §1, §13).
> 사용자의 seam/protect 선택이 source of truth다.

---

## 2. 실행 커맨드 (실제 Blender)

### export_edge_geometry (계획 §5.1)

```bash
blender --background --python worker/seam_editor_worker.py -- --job job.json
# job: {"command":"export_edge_geometry","model":".../SM_Test_Pottery_a_02.fbx",
#       "object_name":"SM_Test_Pottery_a_02","out_dir":"runs/seam_export"}
```

결과: `status=accepted`. `edge_geometry.json` + `export_result.json` 생성.
mesh_signature `vertices 6562 / edges 18701 / faces 12152 / loops 36896`.
edge id가 dense `0..18700`이고 `extract_mesh_graph()`의 bmesh edge index와 동일.
edge0 예: `{id:0, vertex_ids:[71,456], face_ids:[826,0], dihedral_angle:34.31, is_seam:false}`. **PASS**

### extract_uv_boundary_as_seams (계획 §6.4)

```bash
blender --background --python worker/seam_editor_worker.py -- --job job.json
# job: {"command":"extract_uv_boundary_as_seams","object_name":"SM_Test_Pottery_a_02",
#       "uv_layer":"UVChannel_1","out_dir":"runs/seam_boundary",
#       "out_path":".../work/seams/reference_boundary_seam_spec.json"}
```

결과: `status=accepted`, `user_seam_count=724`, `user_protected_count=0`.
report: `mesh_boundary_edges=506`(open edge, 별도 보고), `ambiguous=0`, `non_manifold=0`,
`uv_layer_missing=false`. 생성 spec이 `UserSeamSpec.from_dict()`로 load되고 모든 seam id가
`[0,18701)` 범위 내. protected는 비어 있음(자동 추가 없음). **PASS**

### extract_uv_boundary_as_seams (no-UV)

```bash
# model = vt 없는 cube OBJ
```

결과: `status.json=no_uv`, report `path=null`, boundary spec 미생성. **PASS**

---

## 3. UV Boundary 추출 결과 (pottery `UVChannel_1`)

mesh: vertices 6562 / edges 18701 / faces 12152 / loops 36896

| 항목 | 값 |
| --- | --- |
| boundary seam edges (`user_seam_edges`) | 724 |
| mesh boundary(open) edges (별도 보고) | 506 |
| ambiguous edges | 0 |
| non-manifold edges | 0 |
| user_protected_edges | 0 (MVP 2는 자동 추가 안 함) |
| spec mode | `user_seams` |
| UserSeamSpec.from_dict() load | OK (object 일치, id 전부 in-range) |

> 해석: UV discontinuity(인접 면이 같은 vertex를 다른 UV에 두는 edge)를 seam으로 판정한다.
> open edge는 비교 대상 면이 하나뿐이라 자동 seam에 넣지 않고 report에만 분리 기록한다(계획 §6.4).
> 이 724개 seam은 아티스트가 저장 전 검토/수정하는 **draft**다(계획 §1, §F).

---

## 4. Artifact 검증

| artifact | 위치 | 생성 |
| --- | --- | --- |
| `edge_geometry.json` | `runs/<seam_run>/` | ✅ (renderer의 유일한 selectable id source) |
| `export_result.json` | `runs/<seam_run>/` | ✅ (mesh_signature + artifacts) |
| `status.json` | `runs/<seam_run>/` | ✅ (queued→running→accepted/no_uv/failed) |
| `boundary_extract_report.json` | `runs/<seam_run>/` | ✅ (spec + report + count) |
| `reference_boundary_seam_spec.json` | `work/seams/` | ✅ (draft spec) |
| `user_seam_spec.json` | `work/seams/` | ✅ (canonical, app saveSpec) |

artifact 경로는 project-relative로 summary/result에 기록. Renderer는 normalized JSON만 읽고
stdout을 parse하지 않는다(계획 §13). **PASS**

---

## 5. 자동화 테스트 결과

| 테스트 | 결과 |
| --- | --- |
| `tests/test_edge_geometry_contract.py` | PASS (6) |
| `tests/test_uv_boundary_extract.py` | PASS (5, known-boundary 2-quad fixture 포함) |
| `tests/test_seam_spec_contract.py` | PASS (9, §6.3 예제 + UserSeamSpec round-trip) |
| `tests/e2e/test_mvp2_seam_editor.py` | PASS (3, 실제 Blender pottery export/extract/no_uv) |
| 전체 `pytest` | PASS (599 passed / 1 skipped, 회귀 없음) |
| `npm run typecheck` (node+web) | PASS |
| `npm run build` (three.js 포함 renderer 번들) | PASS |
| `npm run test:integration` (mock worker) | PASS (6, seam export/save/load + boundary 2건 추가) |

> Blender 미설치 환경에서는 `tests/e2e/test_mvp2_seam_editor.py`가 skip 되어 `pytest`가
> green을 유지한다(계획 §11 Session G 정책). known-boundary 검증은 Blender 없는 순수
> 2-quad fixture(`test_uv_boundary_extract.py`)로도 커버한다.

---

## 6. Production Acceptance Checklist (계획 §13)

Functional

- [x] MVP 1 project의 selected object를 seam editor로 열기 (Inspect → Load Working Mesh)
- [x] edge geometry export + UI 표시 (Three.js line overlay)
- [x] UI에서 edge 선택 (screen-space ray-to-segment hit test + fallback edge-id input)
- [x] selected edge → seam mark (`normal/protected → seam`)
- [x] selected edge → protected mark (`normal/seam → protected`)
- [x] selected edge → normal clear
- [x] seam/protected counts 표시
- [x] user seam spec 저장 (`work/seams/user_seam_spec.json`)
- [x] user seam spec 재load
- [x] 기존 UV boundary → seam spec 추출 (724 edges, pottery)
- [x] saved spec이 `project.json.active_user_seam_spec`로 MVP 3 input 기록

Robustness

- [x] Blender 경로 미설정 시 mock(cube) fallback / 실제 경로 시 Blender export
- [x] object mismatch spec은 `object_mismatch=true`로 보고하고 apply gate (계획 §4)
- [x] invalid edge id는 crash 없이 report + 저장 시 제거 (seam-wins normalize)
- [x] mesh signature(`vertices/edges/faces/loops`) 노출로 topology drift 감지 근거 제공
- [x] no-UV model에서 boundary 추출은 `status: no_uv`
- [x] worker 실패가 앱 crash로 이어지지 않음 (structured status/error)
- [x] generated output은 project folder(`runs/`, `work/seams/`)에만 저장

Contract

- [x] 모든 seam command JSON in/out
- [x] export/extract/validate run에 `status.json`
- [x] `user_seam_spec.json`이 `UserSeamSpec.from_dict()`로 load
- [x] Renderer는 자체 edge id 생성 안 함 (`edges[].id`만 사용)
- [x] artifact path는 project-relative로 result에 저장

Quality

- [x] Python tests PASS
- [x] Electron typecheck PASS
- [x] Electron renderer build PASS
- [x] sample pottery boundary extraction smoke 문서화 (본 문서)

---

## 7. Done Definition (계획 §16) 데모 대응

| 단계 | 상태 |
| --- | --- |
| 1. MVP 1 project open | ✅ (main `project:open`) |
| 2. project.json → working model + selected object | ✅ (resolveWorkingModel) |
| 3. Seam Editor workspace open | ✅ (3번째 mode 탭) |
| 4. edge_geometry.json export + load | ✅ (`seam:exportEdgeGeometry`, 18701 edges) |
| 5. 3D view에서 edge 선택 | ✅ (screen-space hit test) |
| 6. seam mark | ✅ |
| 7. 다른 edge protected mark | ✅ |
| 8. seam/protected overlay + counts | ✅ (색상 구분 + 우측 패널) |
| 9. UVChannel_1 boundary 추출 | ✅ (724 seams draft) |
| 10. 추출 count + warning/report 표시 | ✅ (Seam Specs 패널 draft box) |
| 11. user_seam_spec.json 저장 | ✅ (`seam:saveSpec`) |
| 12. project.json → active_user_seam_spec | ✅ (setActiveUserSeamSpec) |
| 13. saved spec이 UserSeamSpec로 load | ✅ (e2e + contract test 검증) |
| 14. MVP 3가 model+object+spec path를 추가 변환 없이 수신 | ✅ (handoff contract §10) |

---

## 8. 알려진 한계 / 후속

- **Renderer 수동 렌더 확인**: Electron UI는 typecheck/build/main 통합테스트(mock worker)로
  검증했다. 실제 창에서의 Three.js viewport orbit/select 인터랙션은 데모 환경에서 별도
  수동 확인을 권장한다(headless 빌드 게이트는 통과).
- **Hit-test 비용**: screen-space ray-to-segment는 프레임당 O(E)다. pottery(18.7k edges)
  규모까지는 실용적이며, tolerance는 UI 슬라이더(3~20px)로 조정 가능하다. 초대형 mesh는
  `edge_geometry_size_warnings`(>250k edges)로 경고하고 fallback edge-id 입력을 제공한다.
- **validate/save/load 경로**: 앱은 renderer가 보유한 edge count로 pure-Node에서 즉시
  normalize/save 한다(Blender 비소환). 동일 규칙을 Python `app_seam_spec_contract.py`와
  TS `seamEditor.ts` 양쪽에 미러링했고, `tests/test_seam_spec_contract.py`의 §6.3 예제로
  교차 검증한다. Blender headless 경로(worker load/save/validate)도 구현되어 있다.
- **chapters**: MVP 2 editor는 chapters를 생성하지 않으며 normalize 시 passthrough한다(계획 §4).
- **mesh boundary(open) edge**: boundary 추출에서 open edge는 자동 seam에 넣지 않고 report에만
  분리 기록한다. 필요 시 후속에서 사용자 선택형 포함 옵션 검토 가능.
