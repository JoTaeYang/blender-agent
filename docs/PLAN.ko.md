# AI Direct UV Layout Agent 작업 계획서

## 1. 프로젝트 개요

### 프로젝트명

AI Direct UV Layout Agent

### 목적

Blender에 로딩된 3D Mesh를 대상으로 AI Agent가 UV seam 선택뿐 아니라 UV island 생성, UV 좌표 배치, overlap 해소, stretch 최소화, packing 최적화를 직접 수행하는 시스템을 개발한다.

최종 목표는 Blender Add-on 단독 도구가 아니라, 웹앱에서 채팅 기반으로 프로젝트별 세션과 메모리를 관리하고, 사용자가 자연어로 UV 작업을 지시할 수 있는 AI UV 제작 환경을 만드는 것이다.

```text
사용자 채팅
  -> Mesh 분석
  -> AI UV 계획 수립
  -> Blender 실행 도구 호출
  -> UV 좌표 직접 수정
  -> 품질 평가
  -> 재작업/메모리 저장
  -> 결과 비교 및 승인
```

## 2. 기존 계획서와의 차이

기존 계획서는 Blender의 `bpy.ops.uv.unwrap()`과 `bpy.ops.uv.pack_islands()`를 호출하고 결과를 평가하는 자동화 Agent에 가깝다.

새 계획서는 AI Agent가 UV 데이터를 직접 다룬다.

| 구분 | 기존 계획 | 새 계획 |
| --- | --- | --- |
| AI 역할 | unwrap 전략 선택 | UV layout 생성/수정 의사결정 |
| 실제 UV 생성 | Blender unwrap operator | UV coordinate 직접 write |
| 핵심 데이터 | seam edge list | UV island graph, loop UV coordinate |
| 수정 단위 | seam threshold, margin | island split, rotate, scale, translate, vertex UV 이동 |
| 목표 | 자동 unwrap | AI 기반 UV layout editor/generator |
| 웹앱 | 제외 | 최종 목표 |

## 3. AI Provider: openai-oauth Proxy 중심 구조

참고 repo `EvanZhouDev/openai-oauth`는 ChatGPT/Codex OAuth 토큰을 사용해 로컬에서 OpenAI 호환 API 프록시를 띄우는 비공식 프로젝트다. README 기준으로 `npx openai-oauth`를 실행하면 `http://127.0.0.1:10531/v1` 형태의 OpenAI-compatible endpoint를 제공한다.

이 프로젝트에서의 적용 방침은 다음과 같다.

### 기본 방침

이 프로젝트는 외부 배포가 아니라 개인 로컬 사용을 전제로 하므로 `openai-oauth` 로컬 프록시를 메인 AI Provider로 사용한다.

```text
Web App Backend
  -> http://127.0.0.1:10531/v1
  -> openai-oauth local proxy
  -> local ChatGPT/Codex OAuth cache
```

### 로컬 실행 전제

개발자는 먼저 Codex/ChatGPT OAuth 로그인을 완료한 뒤 로컬 프록시를 실행한다.

```bash
npx openai-oauth
```

웹앱 백엔드는 OpenAI SDK 또는 OpenAI-compatible client의 `baseURL`을 `http://127.0.0.1:10531/v1`로 설정한다. API key는 필요하지 않거나 dummy value를 사용한다.

### 공식 API key 옵션

OpenAI API key 방식은 나중에 필요할 때 켤 수 있는 fallback provider로만 둔다.

```text
Web App Backend
  -> https://api.openai.com/v1
  -> OPENAI_API_KEY from local env
```

### 제한 및 주의

- `openai-oauth`는 OpenAI 공식 프로젝트가 아니다.
- 로컬 OAuth auth file은 계정 접근 권한과 연결된 민감한 자격 증명으로 취급해야 한다.
- 호스팅 서비스, 팀 공유, 토큰 재배포, 여러 사용자 공동 사용에는 사용하지 않는다.
- 이 계획서는 개인 로컬 실행을 기준으로 하며, 외부 배포나 SaaS화를 목표로 하지 않는다.

## 4. 최종 제품 비전

### 사용자 경험

사용자는 웹앱에서 프로젝트를 열고 채팅으로 지시한다.

```text
"이 로봇 팔 모델 UV를 하드서피스 텍스처링용으로 펴줘."
"볼트 구멍 주변 stretch가 심해. 그 부분 island를 따로 빼줘."
"이전 시도보다 island 수는 줄이고 padding은 8px 기준으로 맞춰줘."
"이 캐릭터 얼굴은 대칭 유지하고 몸통은 텍스처 밀도 우선으로 다시 배치해줘."
```

웹앱은 다음을 제공한다.

- 프로젝트별 채팅 세션
- 모델별 UV 작업 히스토리
- 사용자의 선호 메모리
- Blender 작업 실행 상태
- UV 결과 이미지/점수 비교
- 이전 결과로 rollback
- 특정 island 또는 mesh 영역에 대한 후속 지시

## 5. 전체 시스템 구조

```text
Web App
  ├─ Chat UI
  ├─ Project Dashboard
  ├─ Session Timeline
  ├─ UV Result Viewer
  └─ Memory/Preference UI

Backend API
  ├─ Auth/User Management
  ├─ Chat Session Service
  ├─ Memory Service
  ├─ LLM Gateway
  ├─ Agent Orchestrator
  ├─ Blender Job Queue
  └─ Asset Storage

Blender Worker
  ├─ Blender Add-on Bridge
  ├─ Mesh Extractor
  ├─ UV Direct Editor
  ├─ UV Evaluator
  ├─ Preview Renderer
  └─ Result Exporter

AI UV Agent
  ├─ Mesh Understanding
  ├─ UV Island Planner
  ├─ UV Coordinate Generator
  ├─ Packing Optimizer
  ├─ Quality Critic
  └─ Retry/Repair Planner
```

## 6. 핵심 설계 원칙

### UV 좌표 직접 조작

Blender operator에만 의존하지 않고 UV layer data를 직접 수정한다.

```python
uv_layer = mesh.uv_layers.active.data
uv_layer[loop_index].uv = (u, v)
```

### AI는 좌표를 무제한 자유 생성하지 않는다

LLM이 raw UV 좌표 수천 개를 한 번에 생성하는 구조는 불안정하다. 대신 Agent는 구조화된 action을 생성하고, deterministic geometry engine이 실제 좌표 계산을 수행한다.

```json
{
  "action": "split_island",
  "target_faces": [120, 121, 122],
  "reason": "high curvature and visible texture seam acceptable"
}
```

```json
{
  "action": "pack_island",
  "island_id": "island_07",
  "rotate_deg": 90,
  "scale": 0.82,
  "translate": [0.14, 0.62]
}
```

### LLM + Geometry Solver 혼합

LLM은 계획, 판단, 재시도 전략, 사용자 의도 해석을 담당한다.

Geometry Solver는 실제 UV 좌표 계산, overlap 검사, packing, stretch 계산을 담당한다.

```text
LLM = 의도 이해 + 전략 + repair plan
Solver = 좌표 계산 + 제약 최적화 + 검증
Blender = mesh source + UV write + preview render
```

## 7. 주요 기능 정의

## 7.1 Mesh Extraction

### 기능명

`extract_mesh_graph(obj)`

### 목적

Blender mesh를 AI와 Solver가 사용할 수 있는 구조화 데이터로 변환한다.

### 출력 예시

```json
{
  "object_id": "robot_arm_001",
  "vertex_count": 12450,
  "edge_count": 26890,
  "face_count": 14320,
  "faces": [
    {
      "face_id": 0,
      "vertex_ids": [0, 1, 2, 3],
      "edge_ids": [0, 1, 2, 3],
      "normal": [0, 0, 1],
      "area_3d": 0.031
    }
  ],
  "edges": [
    {
      "edge_id": 12,
      "face_ids": [4, 7],
      "dihedral_angle": 84.2,
      "is_boundary": false,
      "is_non_manifold": false
    }
  ]
}
```

### 작업 내용

- vertex, edge, face, loop 추출
- face adjacency graph 생성
- edge dihedral angle 계산
- boundary/non-manifold 탐지
- material slot, sharp edge, bevel-like region 탐지
- 기존 UV layer가 있으면 UV coordinate 함께 추출

## 7.2 UV Island Planning

### 기능명

`plan_uv_islands(mesh_graph, user_intent, memory)`

### 목적

mesh를 어떤 island로 나눌지 계획한다.

### 입력

- mesh graph
- 사용자 지시
- 프로젝트 메모리
- 이전 UV 결과
- 품질 목표

### 출력 예시

```json
{
  "islands": [
    {
      "island_id": "arm_outer_shell",
      "face_ids": [10, 11, 12, 13],
      "priority": "visible",
      "texel_density": "high",
      "seam_visibility": "avoid_front"
    }
  ],
  "seam_edges": [32, 33, 80, 81],
  "constraints": {
    "preserve_symmetry": true,
    "max_overlap_ratio": 0.0,
    "padding_px": 8
  }
}
```

## 7.3 Direct UV Coordinate Generation

### 기능명

`generate_uv_coordinates(mesh_graph, island_plan)`

### 목적

각 island의 face loop에 UV 좌표를 생성한다.

### 접근 방식

MVP에서는 완전한 신경망 UV 생성이 아니라 다음 조합으로 구현한다.

- island별 local parameterization
- LSCM/ABF 유사 unwrap 알고리즘 또는 Blender unwrap 결과를 초기값으로 사용
- AI가 split/merge/constraint를 지정
- Solver가 coordinate relaxation 수행
- 결과를 Blender UV layer에 직접 write

### 출력 예시

```json
{
  "uv_coordinates": [
    {
      "face_id": 10,
      "loop_index": 48,
      "uv": [0.125, 0.722]
    }
  ],
  "island_transforms": [
    {
      "island_id": "arm_outer_shell",
      "rotation_deg": 90,
      "scale": 0.74,
      "translation": [0.12, 0.18]
    }
  ]
}
```

## 7.4 UV Direct Editor

### 기능명

`apply_uv_coordinates(obj, uv_solution)`

### 목적

계산된 UV 좌표를 Blender mesh의 UV layer에 직접 반영한다.

### 작업 내용

- `AI_UV` layer 생성 또는 선택
- face loop index 검증
- UV coordinate write
- seam flag 동기화
- mesh update
- checker material 적용
- preview render 생성

## 7.5 UV Quality Evaluation

### 기능명

`evaluate_uv_solution(obj, uv_solution)`

### 평가 지표

| 지표 | 설명 |
| --- | --- |
| `overlap_ratio` | island/face 간 UV 겹침 |
| `stretch_score` | 3D 면적 대비 UV 면적 왜곡 |
| `angle_distortion` | 3D angle과 UV angle 차이 |
| `texel_density_variance` | island 간 texel density 편차 |
| `packing_efficiency` | 0-1 UV 공간 사용률 |
| `seam_visibility_score` | 카메라/normal/material 기준 seam 노출 위험 |
| `island_count` | island 개수 |
| `small_island_ratio` | 너무 작은 island 비율 |

### 출력 예시

```json
{
  "overlap_ratio": 0.0,
  "stretch_score": 0.14,
  "angle_distortion": 0.18,
  "texel_density_variance": 0.09,
  "packing_efficiency": 0.76,
  "seam_visibility_score": 0.21,
  "status": "accepted"
}
```

## 7.6 AI Repair Planner

### 기능명

`plan_uv_repair(evaluation, user_feedback, memory)`

### 목적

품질 평가나 사용자 피드백을 바탕으로 UV를 직접 수정한다.

### Action 종류

| Action | 설명 |
| --- | --- |
| `split_island` | island를 더 나눔 |
| `merge_islands` | 작은 island를 병합 |
| `relax_island` | stretch를 줄이기 위해 좌표 재완화 |
| `rotate_island` | packing 개선을 위해 회전 |
| `scale_island` | texel density 조정 |
| `translate_island` | packing 위치 변경 |
| `pin_uv_vertices` | 특정 UV vertex 고정 |
| `protect_region` | 사용자가 지정한 영역을 재작업에서 제외 |
| `repack_all` | 전체 island 재배치 |
| `manual_review_required` | 자동 수정 실패 |

## 8. 웹앱 기능 정의

## 8.1 Project Workspace

### 기능

- 프로젝트 생성
- Blender file 또는 mesh asset 연결
- Object별 작업 상태 표시
- UV 결과 버전 관리
- preview image와 평가 점수 비교

### 주요 화면

- Project list
- Object list
- Chat + UV preview split view
- UV result history
- Memory/preferences panel

## 8.2 Chat Session Management

### 기능

- 프로젝트별 여러 chat session 생성
- session별 message history 저장
- 각 message와 Blender job/result 연결
- LLM 입력 context를 session별로 재구성
- 이전 작업으로부터 follow-up 가능

### 데이터 모델 예시

```text
User
Project
Asset
Object
ChatSession
ChatMessage
AgentRun
BlenderJob
UVResult
MemoryItem
```

## 8.3 Memory System

### Memory 종류

| 종류 | 예시 |
| --- | --- |
| User preference | "하드서피스는 island 수보다 distortion 감소 우선" |
| Project memory | "이 프로젝트는 4K 텍스처 기준 padding 16px 사용" |
| Object memory | "robot_arm_upper는 전면 seam을 피해야 함" |
| Session memory | "이번 세션에서는 smart unwrap보다 직접 island 편집 선호" |
| Failed attempt memory | "angle 45 기반 split은 small island가 과다했음" |

### 구현 방향

- PostgreSQL에 canonical memory 저장
- vector embedding으로 semantic retrieval
- memory importance score와 last_used_at 관리
- 사용자가 memory를 확인/삭제/고정 가능

## 8.4 LLM Gateway

### 목적

local OAuth proxy를 기본 provider로 사용하되, 나중에 official API key provider로 바꿀 수 있도록 같은 인터페이스로 추상화한다.

```ts
interface LLMProvider {
  responsesCreate(input: AgentInput): Promise<AgentOutput>;
  streamChat(input: ChatInput): AsyncIterable<ChatEvent>;
}
```

### Provider 종류

| Provider | 용도 |
| --- | --- |
| `openai_oauth_local` | 기본 provider, 개인 로컬 실행 |
| `openai_api_key` | fallback 또는 향후 전환용 |
| `mock_provider` | 테스트 |

## 9. Blender 연동 방식

## 9.1 로컬 개발 구조

```text
Web App Backend
  -> Job Queue
  -> Local Blender Worker
  -> Blender Python Add-on
  -> UV Result Export
```

## 9.2 Blender Worker

### 역할

- Blender headless 실행
- `.blend` 파일 열기
- 대상 object 선택
- mesh graph 추출
- UV solution 적용
- preview render 생성
- 결과 `.blend`, `.json`, `.png` 저장

### 실행 예시

```bash
blender --background project.blend --python worker/run_uv_job.py -- --job-id job_123
```

## 9.3 Blender Add-on

웹앱 없이도 로컬 Blender에서 사용할 수 있도록 최소 UI를 제공한다.

- Connect to Web App
- Export Mesh Graph
- Apply UV Solution
- Preview Result
- Send Feedback

## 10. AI Agent 설계

## 10.1 Agent 입력

```json
{
  "user_message": "볼트 구멍 주변 stretch 줄여줘",
  "mesh_summary": {},
  "uv_evaluation": {},
  "visible_regions": [],
  "memory": [],
  "available_tools": [
    "extract_mesh_graph",
    "create_island_plan",
    "generate_uv_coordinates",
    "apply_uv_coordinates",
    "evaluate_uv_solution",
    "render_preview"
  ]
}
```

## 10.2 Agent 출력

LLM 출력은 반드시 JSON schema로 제한한다.

```json
{
  "intent": "repair_uv",
  "plan": [
    {
      "tool": "split_island",
      "args": {
        "target_region": "bolt_holes",
        "strategy": "curvature_boundary"
      }
    },
    {
      "tool": "relax_island",
      "args": {
        "target_island": "bolt_hole_ring",
        "preserve_boundary": true
      }
    }
  ],
  "success_criteria": {
    "stretch_score_max": 0.18,
    "overlap_ratio_max": 0.0
  }
}
```

## 11. 기술 스택 제안

### Web App

- Next.js
- TypeScript
- React
- Tailwind CSS 또는 기존 디자인 시스템
- Three.js 또는 Babylon.js for 3D/UV preview

### Backend

- Node.js/NestJS 또는 Next.js API routes
- PostgreSQL
- Prisma
- Redis + BullMQ
- S3-compatible storage
- WebSocket/SSE for job progress

### AI

- `openai-oauth` compatible local provider 기본
- OpenAI official API key provider는 fallback 옵션
- JSON schema validation
- tool-calling based agent loop
- eval suite

### Blender

- Blender Python
- bpy
- bmesh
- headless Blender worker
- add-on bridge

### Geometry/UV Solver

- MVP: Python geometry utilities + Blender data access
- 이후: libigl, xatlas, custom packing optimizer 검토

## 12. MVP 개발 단계

## Phase 1. Direct UV Write Prototype

### 목표

Blender mesh의 UV layer에 좌표를 직접 쓰는 최소 기능을 만든다.

### 산출물

- `apply_uv_coordinates(obj, uv_solution)`
- Cube/Plane 테스트
- UV Editor에서 직접 좌표 변경 확인

### 완료 기준

Blender unwrap operator 없이도 UV 좌표가 변경되어야 한다.

## Phase 2. Mesh Graph Extractor

### 목표

AI와 Solver가 사용할 mesh graph를 추출한다.

### 산출물

- face/edge/loop adjacency
- dihedral angle
- material/visibility metadata
- JSON export

### 완료 기준

선택 Object를 구조화 JSON으로 export할 수 있어야 한다.

## Phase 3. Island Planner MVP

### 목표

규칙 기반으로 island를 생성하고 LLM이 이를 수정할 수 있는 구조를 만든다.

### 산출물

- hard edge 기반 island split
- material boundary 기반 split
- user prompt 기반 target region tagging
- island graph

### 완료 기준

AI가 "이 영역을 따로 빼라"는 지시를 action으로 만들고 island plan에 반영할 수 있어야 한다.

## Phase 4. UV Coordinate Solver MVP

### 목표

island별 초기 UV 좌표를 생성한다.

### 산출물

- planar projection
- cylindrical projection
- Blender unwrap result import as initial solution
- coordinate relaxation
- UV layer write

### 완료 기준

최소한 hard-surface 모델에 대해 island별 UV 좌표가 생성되어야 한다.

## Phase 5. Packing Optimizer

### 목표

island를 0-1 UV space에 직접 배치한다.

### 산출물

- island bounding polygon 계산
- rotate/scale/translate transform
- padding 적용
- overlap-free packing

### 완료 기준

island가 겹치지 않고 지정 padding으로 배치되어야 한다.

## Phase 6. UV Quality Evaluator

### 목표

결과를 수치화하고 AI가 재시도할 수 있게 한다.

### 산출물

- overlap ratio
- stretch score
- texel density variance
- packing efficiency
- preview image

### 완료 기준

각 Agent run마다 score report와 preview가 저장되어야 한다.

## Phase 7. LLM Agent Loop

### 목표

LLM이 tool action을 선택하고 UV 결과를 반복 개선한다.

### 산출물

- prompt
- JSON schema
- tool dispatcher
- retry policy
- failure fallback

### 완료 기준

사용자 채팅 한 번으로 `plan -> execute -> evaluate -> repair` 루프가 동작해야 한다.

## Phase 8. Web App MVP

### 목표

채팅으로 UV 작업을 요청하고 결과를 볼 수 있는 웹앱을 만든다.

### 산출물

- project/session CRUD
- chat UI
- Blender job queue
- result preview
- memory 저장

### 완료 기준

웹앱에서 메시지를 보내면 Blender worker가 UV job을 실행하고 결과 preview를 웹앱에 반환해야 한다.

## Phase 9. Memory System

### 목표

사용자 선호와 프로젝트 규칙을 후속 작업에 반영한다.

### 산출물

- memory extraction
- memory retrieval
- memory edit/delete UI
- prompt context injection

### 완료 기준

이전 세션에서 저장된 UV 선호가 새 세션의 Agent 판단에 반영되어야 한다.

## Phase 10. Productization

### 목표

개인 실험 도구에서 안정적인 웹앱으로 확장한다.

### 산출물

- auth
- permissions
- rate limit
- job isolation
- asset storage lifecycle
- observability
- eval benchmark

## 13. 데이터베이스 초안

```sql
User(id, email, created_at)
Project(id, user_id, name, created_at)
Asset(id, project_id, file_url, file_type, created_at)
Object3D(id, asset_id, name, mesh_hash, metadata_json)
ChatSession(id, project_id, title, created_at)
ChatMessage(id, session_id, role, content, metadata_json, created_at)
AgentRun(id, session_id, message_id, status, provider, model, input_json, output_json, created_at)
BlenderJob(id, agent_run_id, status, worker_id, logs, created_at, completed_at)
UVResult(id, object_id, agent_run_id, version, uv_json_url, blend_url, preview_url, score_json, created_at)
MemoryItem(id, user_id, project_id, object_id, type, content, embedding, importance, created_at, last_used_at)
```

## 14. API 초안

```text
POST /api/projects
GET  /api/projects/:id
POST /api/projects/:id/assets
POST /api/sessions
GET  /api/sessions/:id/messages
POST /api/sessions/:id/messages
POST /api/agent-runs/:id/cancel
GET  /api/blender-jobs/:id
GET  /api/uv-results/:id
POST /api/uv-results/:id/approve
POST /api/memory
GET  /api/memory?projectId=...
DELETE /api/memory/:id
```

## 15. 리스크와 대응

| 리스크 | 대응 |
| --- | --- |
| LLM이 좌표를 부정확하게 생성 | LLM은 action만 생성하고 Solver가 좌표 계산 |
| 복잡한 organic mesh 품질 저하 | hard-surface MVP부터 시작 |
| packing 알고리즘 난이도 | xatlas/libigl 등 검토, MVP는 단순 polygon packing |
| Blender headless 안정성 | job isolation, timeout, log capture |
| OAuth proxy의 비공식성 | 개인 로컬 전용으로 제한하고 외부 접속을 막음 |
| 메모리 오염 | memory review UI, importance score, delete 기능 |
| 로컬 파일 보안 | 프로젝트 디렉터리 격리, 민감 파일 gitignore, 외부 업로드 금지 |

## 16. 1차 MVP 범위

가장 먼저 만들 1차 MVP는 다음으로 제한한다.

- 로컬 웹앱
- 단일 사용자
- 단일 Blender worker
- hard-surface mesh 중심
- `openai-oauth` local proxy 기본 사용
- 채팅 세션 저장
- 프로젝트 메모리 최소 구현
- UV 좌표 직접 write
- preview image 생성
- UV score report 저장

제외 항목:

- 다중 사용자 SaaS
- 완전 자동 organic character UV 보장
- 실시간 3D viewport 편집
- 학습 기반 UV 생성 모델
- 텍스처 베이킹
- 팀 협업 권한 관리

## 17. 성공 기준

1차 MVP는 다음을 만족하면 성공으로 본다.

```text
웹앱에서 사용자가 채팅 입력
  -> Agent가 UV 작업 계획 생성
  -> Blender worker가 mesh graph 추출
  -> UV 좌표를 직접 생성/수정
  -> UV layer에 직접 write
  -> overlap/stretch/packing 평가
  -> preview image 반환
  -> 결과와 대화 내용이 session memory에 저장
```

## 18. 권장 개발 순서

1. Blender에서 UV coordinate direct write 검증
2. mesh graph JSON export 구현
3. hard-surface island planner 구현
4. UV coordinate solver MVP 구현
5. evaluator 구현
6. local Blender worker job runner 구현
7. LLM tool action schema 구현
8. 웹앱 chat/session DB 구현
9. memory retrieval 구현
10. preview/result comparison UI 구현
