# MVP 3 UV Boundary Fallback — QA Results

> 대상 계획: `docs/MVP3_UV_BOUNDARY_FALLBACK_REVISION_PLAN.ko.md`
> 범위: Generate + Optimize의 seam source 입력 정책 변경 (explicit spec / UV boundary derived / needs_input)
> 작성: 구현 세션 통합 검증 · 2026-06-21

---

## 1. 검증 환경

| 항목 | 값 |
| --- | --- |
| Blender | 5.0.1 (`/Applications/Blender.app`) |
| Python | pytest (`tests/`, `tests/e2e/`) |
| Node | tsx `--test` (`app/test/integration.test.ts`), `tsc` typecheck (node+web) |
| 샘플 | `sample/SM_Test_Pottery_a_02.fbx` (UVChannel_1) |

핵심 변경: `active_user_seam_spec`이 없다는 이유만으로 막지 않는다. 기존 UV layer가
있으면 UV island boundary를 derived seam spec으로 추출해 진행하고, spec도 UV layer도
없을 때만 `needs_input`을 반환한다.

---

## 2. 실제 Blender 검증 (UV boundary fallback)

```bash
blender --background --python worker/generate_uv_from_seams.py -- --job job.json
# job: seam_spec=null, uv_layer="UVChannel_1",
#      seam_source_policy="prefer_spec_then_uv_boundary"
```

결과 (`uv_generate_summary.json`):

```json
{
  "status": "accepted",
  "seam_spec": "work/seams/derived_from_uv_boundary.json",
  "seam_source": {
    "type": "uv_boundary_derived",
    "path": "work/seams/derived_from_uv_boundary.json",
    "uv_layer": "UVChannel_1",
    "user_confirmed": false,
    "derived": true
  },
  "seam_integrity": {
    "user_seam_count": 724,
    "user_protected_count": 0,
    "final_seam_count": 724,
    "auto_added_seams": 0,
    "valid": true
  },
  "selected_uv_model": "work/uv/selected_uv.blend"
}
```

확인 사항:

- `work/seams/derived_from_uv_boundary.json` 작성됨 — `object=SM_Test_Pottery_a_02`,
  `mode=user_seams`, `user_seam_edges=724`, `user_protected_edges=[]`,
  `notes="Derived from UV island boundaries: UVChannel_1"`.
- run folder에 `derived_from_uv_boundary.json` + `seam_source_resolution.json` 사본 존재.
- `auto_added_seams == 0`, `final_seam_count == user_seam_count == 724` (seam integrity 유지).
- 6개 before/after preview + `selected_uv.blend` 생성, accepted run이 `work/uv/`로 ship.
- `active_user_seam_spec`은 변경되지 않음 (derived는 별도 파일).

---

## 3. e2e smoke (`tests/e2e/test_mvp3_uv_generate.py`, 실제 Blender)

| 테스트 | 결과 |
| --- | --- |
| `test_missing_seam_spec_smoke` (spec 없음 + uv_layer 없음) | `status=needs_input`, `code=missing_seam_source`, ship 없음 ✔ |
| `test_uv_boundary_fallback_generate_smoke` (spec 없음 + UVChannel_1) | `seam_source.type=uv_boundary_derived`, `auto_added_seams=0`, selected UV 생성 ✔ |
| `test_invalid_seam_spec_smoke` (out-of-range edge) | `status=failed`, `code=invalid_seam_spec`, ship 없음 ✔ |
| `test_reference_boundary_generate_smoke` (explicit spec 우선) | explicit spec 사용, integrity 유지 ✔ |

---

## 4. 단위 / 통합 테스트

- `tests/test_uv_generate_contract.py` — `decide_seam_source`(explicit 우선 / UV fallback /
  needs_input), `build_seam_source`, `make_derived_seam_spec`(protected=[], `UserSeamSpec.from_dict`
  round-trip), summary에 `seam_source` 포함, `needs_input` terminal status. ✔
- `tests/test_uv_generate_seam_integrity.py` — derived 경로 integrity 유지. ✔
- `tests/test_uv_boundary_extract.py` — boundary edges → `make_derived_seam_spec` → `UserSeamSpec`. ✔
- `app/test/integration.test.ts` — derived fallback: validate `ready/seam_source=derived`,
  job에 `uv_layer` 전달, accepted run이 `seam_source.type=uv_boundary_derived` 기록 +
  `latest_derived_seam_spec` 설정 + `active_user_seam_spec` 미변경; missing 소스는 not ready. ✔
- 전체 `pytest -q` green, `npm run typecheck` (node+web) green, `npm run test:integration` 12/12 green.

---

## 5. Acceptance (계획 §7) 충족

- [x] Generate + Optimize가 더 이상 `active_user_seam_spec`을 요구하지 않음.
- [x] selected UV layer가 있으면 derived seam spec을 생성·사용.
- [x] explicit `active_user_seam_spec`이 여전히 우선.
- [x] spec도 UV layer도 없으면 `needs_input`.
- [x] derived spec은 별도 저장되며 user spec을 덮어쓰지 않음.
- [x] summary가 seam source를 명확히 기록.
- [x] derived 경로 `auto_added_seams == 0`, `final_seam_count == user_seam_count`, mandatory 90 report-only.
- [x] Generate UI가 "Explicit seam spec" / "Derived from UV boundary" / "Missing seam source" 표시,
      UV layer만 있어도 Generate 활성화.
