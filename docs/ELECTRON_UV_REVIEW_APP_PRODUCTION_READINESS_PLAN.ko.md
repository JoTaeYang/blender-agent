# Electron UV Review App Production Readiness Plan

> 기준 PRD: `docs/ELECTRON_UV_REVIEW_APP_PRD.ko.md`
> 선행 계약: `docs/ELECTRON_UV_REVIEW_APP_MVP0~MVP5_PRODUCTION_PLAN.ko.md` (MVP 0/1/2/3/5 구현 완료분)
> 범위: 기능 MVP(0/1/2/3/5)가 끝난 `app/`을 **배포 가능한 1.0 데스크톱 제품**으로 끌어올리는 하드닝 계획. 새 UV 기능은 추가하지 않는다.
> 대상: Electron app 작업자, 배포/릴리스 작업자, QA 작업자
> 핵심 목표: 서명·공증된 배포물 + 크래시 안전성 + 접근성 + 보안 하드닝 + 사용자 피드백을 갖춘 production 후보를 만든다.

---

## 0. 현재 상태 진단

기능은 동작하지만 "잘 만든 내부 MVP" 단계다. 코드 재작성이 필요한 결함은 거의 없고, 갭은 **제품화 레이어**(배포·접근성·피드백·온보딩·보안 하드닝)에 있다.

이미 production급인 것:

- 프로세스 격리: `contextIsolation:true`, `nodeIntegration:false` (`app/electron/main/index.ts:24`), preload는 타입드 thin bridge 1개.
- CSP 존재 (`app/electron/renderer/index.html:6`).
- 타입 안전 IPC 계약 (`app/shared/contracts/*`). 렌더러는 정규화 JSON + artifact 경로만 읽고 Blender stdout을 파싱하지 않는다.
- 런 생명주기 하이브리드(1s 폴링 + `onRunUpdate` 푸시, terminal-status에서 폴링 중단), useEffect 정리.
- Mock 러너로 Blender 없이 앱+테스트 전체 구동 (`app/electron/main/worker-runner.ts:217`).
- i18n(en/ko) 347키, 정직한 UX 카피.
- 메인프로세스 통합테스트 11개 (`app/test/integration.test.ts`).

출시를 막는 갭(이 계획의 대상):

| # | 갭 | 근거 | 우선 |
|---|---|---|---|
| 1 | 배포/패키징 전무(electron-builder·아이콘·서명·공증·자동업데이트 0) | `app/package.json`에 dist 스크립트·의존성 없음 | P0 |
| 2 | 전역 에러 처리/복구 부재(React ErrorBoundary 없음, 메인 핸들러 없음) | `app/electron/renderer/src/main.tsx`, grep 결과 `NONE` | P0 |
| 3 | 접근성: 리스트 28개가 `<li onClick>`(키보드 불가), `aria-label` 1개 | 렌더러 전수 grep | P0 |
| 4 | `uvpreview://` 임의 파일 읽기, 내비게이션 하드닝 없음 | `app/electron/main/index.ts:39` | P1 |
| 5 | 피드백 빈약: 배너 슬롯 1개, 진행률 없음, 네이티브 prompt/confirm | `App.tsx:25,84,143`, `ExportWorkspace.tsx:178` | P1 |
| 6 | 프로젝트 인메모리뿐(최근 프로젝트 없음), 첫 실행 온보딩 없음 | `app/electron/main/ipc.ts:69`, `App.tsx:23` | P1 |
| 7 | 제품감/폴리시(아이콘·단축키·창 상태·Blender 경로 UX) | 전반 | P2 |

전체 진단 근거는 본 세션의 리뷰 결과를 따른다.

---

## 1. Production-ready 정의

이 단계의 제품 완료 상태:

```text
소스 코드(app/)
  -> electron-builder로 서명·공증된 설치물(.dmg/.exe) 생성
  -> 깨끗한 머신에서 Gatekeeper/SmartScreen 통과 실행
  -> 번들된 worker/로 실제 Blender 런 동작
  -> 런타임 에러가 앱을 죽이지 않고 복구 UI 제공
  -> 키보드/스크린리더로 핵심 플로우 완주 가능
  -> 보안 체크리스트(파일 접근 클램프·내비 차단) 충족
  -> 장기 작업에 진행률/경과시간/토스트 피드백
  -> 재시작 후 최근 프로젝트 복원
  -> 자동 업데이트로 새 버전 배포
```

반드시 보장할 것:

- 서명·공증된 배포물이 재현 가능하게 빌드된다.
- 패키지 내부에서 worker가 발견되고 실제 Blender 런이 동작한다.
- 렌더러/메인 어디서 throw가 나도 앱이 살아남고 사용자에게 복구 경로를 준다.
- 마우스 없이 import→inspect→선택→review까지 완주 가능하다.
- `uvpreview://`는 허용 루트 밖 파일을 거부한다.
- 모든 알림이 토스트 큐로 누락 없이 전달된다.

하지 않을 것(범위 밖):

- 새 UV/seam/export 알고리즘, MVP 4 AI Review 구현.
- 클라우드 동기화·계정·결제.
- Blender 자체 번들(사용자 설치 가정 유지) — 단, 미설정 시 명확한 안내는 포함.
- 풀 디자인 리브랜딩(아이덴티티 정리는 P2 수준).

---

## 2. Phase A — P0 (출시 차단 해소)

### A1. 배포 파이프라인 (electron-builder + worker 번들)  `[L]`

- **목적:** 서명·공증된 설치물을 만들고, 패키지 안에서 Python/Blender worker를 찾게 한다.
- **변경 파일:** `app/package.json`(devDep `electron-builder`, scripts `dist`/`dist:mac`/`dist:win`), 신규 `app/electron-builder.yml`, 신규 `app/build/`(icon.icns/icon.ico/icon.png, entitlements.mac.plist), `app/electron/main/ipc.ts:53`(`resolveWorkerRoot`).
- **구현:**
  - appId(예: `co.thakicloud.uvreview`), productName, mac target `dmg`+`zip`, win `nsis`.
  - `extraResources`로 repo `worker/`를 패키지에 복사. `resolveWorkerRoot()` 후보에 `process.resourcesPath/worker`를 추가(현재는 repo 상대경로만 탐색하므로 패키징 시 실패).
  - mac: `hardenedRuntime:true` + entitlements(자식 프로세스 spawn 허용), `notarize`(Apple ID/Team ID는 env).
  - 코드사이닝: mac `CSC_LINK`/`CSC_KEY_PASSWORD`, win 인증서. CI에서 secret 주입.
- **수용 기준:** `npm run dist:mac`이 서명+공증된 `.dmg`를 산출. **다른** 머신에서 Gatekeeper 통과 후 실행. 패키지에서 실제 Blender 런(inspect→generate, mock 아님) 1회 성공.
- **검증:** 별도 머신/VM 설치 스모크 + `spctl -a -vv`/`codesign --verify`.
- **의존:** 조직 Apple Developer 계정·인증서(§7 사전 준비물).

### A2. 앱 아이덴티티 정리  `[S]`

- **목적:** "MVP 0" 잔재 제거, 버전 체계 정립.
- **변경 파일:** `app/electron/main/index.ts:21`(window title), `app/electron/renderer/index.html:10`(`<title>`), `app/package.json`(name/version `1.0.0-rc.1`/description).
- **수용 기준:** UI·창·about 어디에도 "MVP 0" 노출 없음. 버전이 한 곳(package.json)에서 파생.
- **검증:** grep `MVP 0` → 코드 0건(문서 제외).

### A3. 자동 업데이트  `[M]`

- **목적:** 출시 후 패치 배포 경로 확보.
- **변경 파일:** `app/package.json`(`electron-updater`), 신규 `app/electron/main/updater.ts`, `app/electron/main/index.ts`(init), `electron-builder.yml`(publish provider).
- **구현:** `electron-updater` + GitHub/Generic provider, `checkForUpdatesAndNotify`. 업데이트 가용/다운로드/재시작은 인앱 토스트(B2)로 노출.
- **수용 기준:** 상위 버전 publish 시 앱이 감지→다운로드→재시작 설치.
- **검증:** 테스트 채널에 두 버전 publish 후 업그레이드 1회.
- **의존:** A1(미서명 업데이트는 mac에서 거부됨).

### A4. 전역 에러 처리 & 복구  `[M]`

- **목적:** 단일 throw가 화이트스크린/프로세스 종료로 번지지 않게 한다.
- **변경 파일:** 신규 `app/electron/renderer/src/ErrorBoundary.tsx` + `app/electron/renderer/src/main.tsx`(App 래핑), `app/electron/main/index.ts`(`process.on('uncaughtException'|'unhandledRejection')`), `app/electron/main/ipc.ts`(핸들러 공통 try/catch 래퍼로 에러 정규화).
- **구현:**
  - ErrorBoundary: `getDerivedStateFromError`+`componentDidCatch` → 폴백 UI(에러 요약 + "다시 시도"/"로그 폴더 열기"/"복사"), i18n 적용.
  - 메인: 핸들러에서 로그 파일 기록(`app.getPath('logs')`) + `dialog.showErrorBox`, 프로세스는 생존.
  - IPC: throw를 `{code,message}`로 정규화해 렌더러 `guard()`가 일관 표시.
- **수용 기준:** 워크스페이스 인위적 throw → 폴백 UI + 복구. 메인 unhandled rejection → 앱 생존 + 로그 1건.
- **검증:** throw 주입 수동 확인 + ErrorBoundary 유닛테스트.

### A5. 접근성 기초 (키보드 + ARIA)  `[L]`

- **목적:** 마우스 없이 핵심 플로우를 조작 가능하게.
- **변경 파일:** 신규 공용 `app/electron/renderer/src/components/SelectableList.tsx`(또는 기존 `.list li` 패턴 교체), 5개 워크스페이스 좌패널 + export history/rollback 리스트, `app/electron/renderer/src/App.tsx`(modetabs), `app/electron/renderer/src/styles.css`(`:focus-visible`).
- **구현:**
  - 리스트: 컨테이너 `role="listbox"`, 행 `role="option" tabIndex=0 aria-selected`, `onKeyDown`(Enter/Space 선택, ↑/↓ 이동).
  - 탭바: `role="tablist"/"tab"`+화살표 이동, 본문 `role="tabpanel"`.
  - 기호 버튼(zoom +/−/reset 등) `aria-label` 보강. severity는 색+텍스트 병기 확인.
  - `:focus-visible` 가시 포커스 링 추가.
  - 3D 뷰포트 키보드 대체 경로: 기존 edge-id 입력(`SeamEditorWorkspace.tsx`)을 공식 대안으로 문서화 + 포커스 가능화.
- **수용 기준:** 키보드만으로 import→inspect→object 선택→layer 선택→review 완주. axe 자동검사 critical 0.
- **검증:** `@axe-core/playwright` 또는 수동 키보드 워크스루 + VoiceOver 스모크.

---

## 3. Phase B — P1 (보안·피드백·영속)

### B1. uvpreview 경로 클램프 + 내비게이션 하드닝  `[M]`

- **목적:** 임의 로컬 파일 읽기·원격 콘텐츠 로드 경로 차단.
- **변경 파일:** `app/electron/main/index.ts`(protocol.handle + webContents 핸들러), 신규 경로검증 유틸 + 유닛테스트.
- **구현:**
  - `uvpreview`: `realpathSync(filePath)`가 허용 루트(projectsRoot + 각 project dir) prefix 안인지 검증, 벗어나면 거부. 확장자 화이트리스트(.png/.jpg).
  - `win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))`(필요 링크는 `shell.openExternal`).
  - `will-navigate` preventDefault(dev URL 예외).
  - `sandbox:true` 전환 검토(preload가 `@shared` import만 → 번들되므로 가능). 불가 시 사유 문서화.
- **수용 기준:** projectsRoot 밖 경로 요청 거부. 외부 링크/네비 차단. 보안 체크리스트 충족.
- **검증:** 경로 클램프 유닛테스트 + 잘못된 경로 수동 요청.

### B2. 토스트 알림 시스템  `[M]`

- **목적:** 단일 배너의 메시지 유실/경쟁 해소.
- **변경 파일:** 신규 `app/electron/renderer/src/components/Toast.tsx`+context, `app/electron/renderer/src/App.tsx`(banner 대체/병행), `styles.css`.
- **구현:** 큐형 토스트(info/success/warn/error), auto-dismiss+수동 닫기, 스택, 액션 버튼(예: "파일 표시"), `aria-live="polite"`. 기존 `setBanner` 호출부를 마이그레이션(영속 인라인 알림은 배너 유지, 일시 알림은 토스트).
- **수용 기준:** 연속 액션 알림이 서로 지우지 않음. 스크린리더가 토스트를 읽음.
- **검증:** 컴포넌트 테스트 + 수동.

### B3. 진행률 & 장기 작업 피드백  `[M]`

- **목적:** 수 분 걸리는 generate/export에서 무응답 화면 제거.
- **변경 파일:** 5개 워크스페이스 center, 공용 `ProgressOverlay`/스켈레톤, (선택) worker `status.json` 스키마.
- **구현:**
  - 최소(앱 단독): indeterminate 스피너 + 경과시간 타이머 + 상태 텍스트(queued/running), 이미지/뷰포트 로딩 스켈레톤, 실행 중 중복 트리거 차단 일관화.
  - 확장(worker 협업, 후속): `status.json`에 `progress:{phase,pct}` 추가 → determinate 바.
- **수용 기준:** generate/export 실행 중 경과시간+단계가 보이고, 응답없는 빈 화면이 없음.
- **검증:** 실제 Blender 장기 런 수동.
- **의존:** determinate 단계는 worker 작업자 협업.

### B4. 프로젝트 영속 + 최근 항목 + 첫 실행 온보딩  `[M]`

- **목적:** 재시작 마찰 제거, 첫 사용자의 죽은 화면 해소.
- **변경 파일:** `app/electron/main/settings.ts`(recentProjects), `app/electron/main/ipc.ts`(open 시 기록 + list IPC), `app/electron/preload/index.ts`, `app/electron/renderer/src/App.tsx`(온보딩).
- **구현:**
  - electron-store에 `recentProjects:{dir,name,openedAt}[]`(상한 N).
  - 프로젝트 미오픈 시 워크스페이스 대신 온보딩(히어로 "모델 가져오기" CTA + 최근 프로젝트 카드 + Blender 미설정 경고).
  - (선택) macOS "Open Recent" 메뉴 연동.
- **수용 기준:** 재시작 후 최근 프로젝트 1클릭 재오픈. 첫 실행에 명확한 시작 지점.
- **검증:** recent 영속 통합테스트 + 수동.

---

## 4. Phase C — P2 (제품감/폴리시)

### C1. Blender 경로 설정 UX  `[M]`

- **목적:** 가장 중요한 셋업 단계의 취약성 제거(`window.prompt` 자유 입력 → `App.tsx:143`).
- **변경 파일:** `app/electron/renderer/src/App.tsx`(SettingsBar), 신규 설정 모달, `app/electron/main/ipc.ts`(pickFile 재사용 + `blender --version` 검증 IPC).
- **구현:** 파일 피커 + "테스트" 버튼(`--version` spawn 결과 표시) + 자동감지 결과 노출. prompt 제거.
- **수용 기준:** 경로를 피커로 선택·즉시 유효성 확인. 잘못된 경로 조기 차단.

### C2. 인앱 모달 시스템 (네이티브 confirm 대체)  `[S]`

- **변경 파일:** 신규 `app/electron/renderer/src/components/Modal.tsx`, `app/electron/renderer/src/export/ExportWorkspace.tsx:178`(롤백 confirm 교체).
- **수용 기준:** 네이티브 `confirm`/`prompt` 호출 0건(grep).

### C3. 가이드 스테퍼 / 워크플로 내비  `[M]`

- **목적:** 영구 disabled "next" placeholder("고장난 듯") 제거, 다음 행동 자명화.
- **변경 파일:** `app/electron/renderer/src/App.tsx`(modetabs → 단계 진행 표시), 각 워크스페이스 subtoolbar의 disabled placeholder 버튼 제거.
- **구현:** approved lowpoly→review→seam→generate→export 전제조건 충족도를 완료/현재/잠김으로 시각화, 잠긴 단계는 이유 툴팁.
- **수용 기준:** 다음 단계가 자명. 영구 비활성 버튼 제거.

### C4. 시각 폴리시 & 데스크톱 기본기  `[M]`

- **변경 파일:** `styles.css`, `app/electron/main/index.ts`(메뉴/단축키/창 상태).
- **구현:** 인라인 SVG 아이콘 세트, 트랜지션, 창 크기/위치 영속, 메뉴 accelerator 단축키, 하단 패널의 `code:message` 개발자 문구를 사용자향으로 정리, (선택) 시스템 테마.
- **수용 기준:** 주요 액션 단축키 동작, 창 상태 복원, 개발자 문구 비노출.

---

## 5. 횡단 항목 (테스트 · CI · 로깅 · 문서)

- **렌더러 테스트:** vitest + `@testing-library/react`로 핵심 컴포넌트(토스트·ErrorBoundary·SelectableList·워크스페이스 상태머신) 단위 테스트. 현재 렌더러 테스트 0.
- **E2E:** Playwright for Electron으로 5대 플로우(import→inspect→generate→approve / uv review / seam save / uv generate / export+rollback) + axe 접근성 자동검사.
- **CI:** `typecheck` + `test:integration` + 렌더러 테스트 + (태그 시) `dist` 아티팩트 빌드. PR 게이트.
- **로깅:** 구조화 로그 파일(`app.getPath('logs')`) + "로그 폴더 열기" 메뉴. (선택) 옵트인 크래시 리포팅.
- **문서:** 본 계획 진행 결과를 `docs/PRODUCTION_READINESS_QA_RESULTS.ko.md`에 기존 `MVPx_QA_RESULTS.ko.md` 패턴으로 기록.

---

## 6. 마일스톤 · 순서 · 의존성

| 마일스톤 | 포함 | 산출물(게이트) |
|---|---|---|
| **M1 — 출시 게이트** | A1, A2, A4, A5, B1 | 서명·공증 배포물 + 크래시 안전 + 접근성 기초 + 보안 하드닝 |
| **M2 — 사용성** | B2, B3, B4, A3 | 토스트·진행률·최근 프로젝트·자동 업데이트 |
| **M3 — 폴리시** | C1, C2, C3, C4 + §5 테스트/CI | 제품감 완성 + 회귀 방지 |

권장 순서: **A2 → A4 → A1 → B1 → A5 → A3 → B2 → B4 → B3 → C2 → C1 → C3 → C4 → §5**.

주요 의존성:

- A3(자동업데이트) ← A1(서명).
- A1(패키지 worker 발견) ↔ `resolveWorkerRoot()` 수정은 한 쌍.
- B3 determinate 진행률 ← worker `status.json` 변경(별 작업자, 후속 가능).
- B1 `sandbox:true` ← preload 번들 검증.

---

## 7. 리스크 & 사전 준비물

- **Apple Developer 계정/인증서·Windows 코드사이닝 인증서**: A1/A3의 하드 의존. 조직 차원 준비 필요(가장 먼저 확보).
- **worker 배포 가정**: Blender는 사용자 설치 가정 유지, 패키지엔 `worker/` 스크립트만 포함. worker가 Blender 내장 Python을 쓰는지 확인 필요(외부 pip 의존이면 별도 번들 전략 필요) — A1 착수 전 검증 항목.
- **공증 시간/CI 비용**: notarize는 수 분~수십 분 소요, 릴리스 파이프라인에 반영.
- **sandbox 전환 리스크**: preload가 번들 외 동적 의존을 끌면 `sandbox:true` 불가 — 검증 후 결정.
- **토스트 마이그레이션 범위**: `setBanner` 호출부 전수 치환 — 누락 시 알림 유실. grep 기준 일괄 처리.

---

## 8. 완료 정의(DoD) & QA 게이트

전 항목 공통 DoD:

- `npm run typecheck` 통과(node+web).
- 관련 단위/통합/E2E 테스트 추가 및 통과.
- 수용 기준의 수동 검증 1회 수행 후 `docs/PRODUCTION_READINESS_QA_RESULTS.ko.md`에 기록.
- 새 사용자향 문자열은 en/ko 모두 추가(하드코딩 금지).

M1 출시 게이트(하나라도 미충족 시 출시 보류):

1. 서명·공증된 배포물이 **별도 머신**에서 Gatekeeper/SmartScreen 통과 실행.
2. 패키지에서 실제 Blender 런 1회 성공.
3. 렌더러/메인 throw 주입 시 앱 생존 + 복구 UI/로그.
4. 키보드만으로 핵심 플로우 완주, axe critical 0.
5. `uvpreview://` 허용 루트 밖 요청 거부 + 외부 내비 차단.

---

## 부록 A. 작업 ↔ 갭 추적표

| 갭(§0) | 해소 작업 |
|---|---|
| 1 배포/패키징 | A1, A2, A3 |
| 2 전역 에러 처리 | A4, §5 로깅 |
| 3 접근성 | A5, §5 axe |
| 4 보안 하드닝 | B1 |
| 5 피드백(배너/진행률/네이티브 다이얼로그) | B2, B3, C1, C2 |
| 6 프로젝트 영속/온보딩 | B4 |
| 7 제품감/폴리시 | C3, C4 |
