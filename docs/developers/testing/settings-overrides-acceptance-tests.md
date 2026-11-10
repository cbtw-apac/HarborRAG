# Test Acceptance Criteria cho Runtime Settings Overrides (AC1 - AC6)

Tài liệu này hướng dẫn cách test 6 acceptance criteria (AC1-AC6) của tính năng
runtime setting overrides (`setting_overrides`, `setting_scope_revisions`,
`/v1/admin/settings*`). Mỗi AC có 2 cách: chạy **pytest tự động** (khuyến
nghị, nhanh, deterministic) hoặc test **thủ công qua Postman** (copy
method/URL/headers/body dưới đây vào từng request) trên stack đang chạy.

## 0. Chuẩn bị

### Cách 1 — pytest (không cần stack chạy, dùng fakes + Postgres test DB)

```bash
# AC1 + AC2 (đọc theo level, cascade/effective)
uv run pytest packages/harborrag-app/tests/test_api_admin_settings.py -k "ac1 or ac2" -v

# AC3 (patch + bump revision)
uv run pytest packages/harborrag-app/tests/test_api_admin_settings.py -k ac3 -v

# AC4 (dry_run + xoá override bằng null)
uv run pytest packages/harborrag-app/tests/test_api_admin_settings.py -k ac4 -v

# AC5 (các trường hợp bị từ chối)
uv run pytest packages/harborrag-app/tests/test_api_admin_settings.py -k ac5 -v

# AC6 (seed lần đầu + drift, không re-apply)
uv run pytest packages/harborrag-runtime/tests/unit/test_settings_seed.py -v
```

Toàn bộ test suite liên quan (chạy 1 lần cho chắc):

```bash
uv run pytest \
  packages/harborrag-runtime/tests/unit/test_settings_seed.py \
  packages/harborrag-app/tests/test_settings_service.py \
  packages/harborrag-app/tests/test_api_admin_settings.py \
  packages/harborrag-app/tests/test_api_settings_seed.py \
  packages/harborrag-adapters/tests/repositories/integration/test_setting_scope_revision_repository.py \
  packages/harborrag-adapters/tests/repositories/integration/test_setting_override_repository.py \
  -q
```

### Cách 2 — test thủ công bằng Postman

Cần:

- Stack đang chạy (`./scripts/deployment/dev.sh up`, hoặc sau khi sửa code API
  thì `./scripts/deployment/dev.sh api --build` để rebuild + restart). API
  nghe ở `http://localhost:8000` (xem `HARBORRAG_API_PORT` trong
  `env/.env.api`).
- **Một tenant test riêng, cô lập hoàn toàn khỏi dữ liệu thật của `DEFAULT`.**
  Tenant trong hệ thống này **không phải một entity phải đăng ký** — nó chỉ
  là chuỗi lấy từ claim `tenants` trong JWT
  (`packages/harborrag-app/src/harborrag_app/api/auth/tenancy.py:18-50`), nên
  chỉ cần mint token với `tenants: ["qa"]` là coi như "có" tenant `qa`. Dưới
  đây dùng tenant test `qa`, không đụng gì tới dữ liệu `DEFAULT`.

#### Mint token (3 role, tenant `qa`)

Lifetime phải ≤ `HARBORRAG_AUTH_MAX_TOKEN_LIFETIME_SECONDS` (mặc định server
đang chạy là **3600s = 1 giờ** nếu biến này không set trong `env/.env.api` —
set dài hơn sẽ bị server trả `401 invalid token lifetime`).

```bash
cat > /tmp/mint_tokens.py <<'PY'
import datetime
import jwt

SECRET = "<dán HARBORRAG_AUTH_SECRET trong env/.env.api vào đây>"


def token(role: str, tenants: list[str]) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return jwt.encode(
        {
            "sub": "tester",
            "role": role,
            "tenants": tenants,
            "iat": now - datetime.timedelta(seconds=1),
            "exp": now + datetime.timedelta(minutes=59),
            "iss": "harborrag",
            "aud": "harborrag-api",
        },
        SECRET,
        algorithm="HS256",
    )


for role in ("admin", "editor", "reader"):
    print(f"{role.upper()}={token(role, ['qa'])}")
PY
uv run python /tmp/mint_tokens.py
```

Token hết hạn sau ~1 giờ — hết hạn thì chạy lại script. Trong Postman: tạo 3
**Collection Variables** `admin_token`/`editor_token`/`reader_token`, dán giá
trị vào, rồi ở mỗi request chọn **Authorization → Type: Bearer Token →
Token: `{{admin_token}}`** (hoặc `{{editor_token}}`/`{{reader_token}}`).

#### Dựng fixture: 2 project + 2 connection, toàn bộ qua API thật (không SQL)

**1) Tạo project `qa-project`**

- Method: `PUT`
- URL: `http://localhost:8000/v1/admin/projects/qa-project`
- Auth: Bearer `admin_token`
- Headers: `Content-Type: application/json`
- Body: `{ "project_name": "QA Project" }`
- Kỳ vọng: `201`

**2) Tạo project thứ 2 `qa-project-2`** (dùng cho case "project khác" ở AC2)

- Method: `PUT`
- URL: `http://localhost:8000/v1/admin/projects/qa-project-2`
- Auth: Bearer `admin_token`
- Headers: `Content-Type: application/json`
- Body: `{ "project_name": "QA Project 2" }`
- Kỳ vọng: `201`

**3) Tạo connection `qa-conn-a` trong `qa-project`** (dùng connector
`local_file`, không cần credential, `source_path` phải nằm trong
`LOCAL_SOURCE_PATH` đã mount — mặc định `/data/sources`)

- Method: `PUT`
- URL: `http://localhost:8000/v1/admin/projects/qa-project/connections/qa-conn-a`
- Auth: Bearer `admin_token` (hoặc `editor_token`)
- Headers: `Content-Type: application/json`
- Body:
  ```json
  {
    "source_type": "local_file",
    "name": "QA Conn A",
    "credentials": {},
    "config": { "source_path": "/data/sources" }
  }
  ```
- Kỳ vọng: `201`

**4) Tạo connection `qa-conn-b` trong `qa-project`** (sibling, dùng cho AC2)

- Method, Auth, Headers: giống bước 3
- URL: `http://localhost:8000/v1/admin/projects/qa-project/connections/qa-conn-b`
- Body: giống bước 3, đổi `"name": "QA Conn B"`
- Kỳ vọng: `201`

Đã verify trực tiếp 4 request này bằng curl — tạo thành công, và toàn bộ
cascade AC1/AC2 bên dưới chạy đúng trên fixture này.

Mỗi request dưới đây ghi theo format: **Method**, **URL**, **Headers** (ngoài
Authorization), **Body** (raw JSON, nếu có), và **Kỳ vọng**.

---

## AC1 — Đọc 1 level

### Fixture: tạo cascade 512 / 384 / 256 trước (copy 3 request PATCH này)

**1) Set tenant = 512**

- Method: `PATCH`
- URL: `http://localhost:8000/v1/admin/settings?level=tenant`
- Auth: Bearer `admin_token`
- Headers: `If-Match: W/"rev-0"`, `Content-Type: application/json`
- Body:
  ```json
  { "chunking.max_tokens": 512 }
  ```

**2) Set project qa-project = 384**

- Method: `PATCH`
- URL: `http://localhost:8000/v1/admin/settings?level=project:qa-project`
- Auth: Bearer `admin_token`
- Headers: `If-Match: W/"rev-0"`, `Content-Type: application/json`
- Body:
  ```json
  { "chunking.max_tokens": 384 }
  ```

**3) Set connection qa-project/qa-conn-a = 256**

- Method: `PATCH`
- URL: `http://localhost:8000/v1/admin/settings?level=connection:qa-project/qa-conn-a`
- Auth: Bearer `admin_token`
- Headers: `If-Match: W/"rev-0"`, `Content-Type: application/json`
- Body:
  ```json
  { "chunking.max_tokens": 256 }
  ```

### Các request test AC1

**1) Admin đọc level project**

- Method: `GET`
- URL: `http://localhost:8000/v1/admin/settings?level=project:qa-project`
- Auth: Bearer `admin_token`
- Kỳ vọng: `200`, header `ETag: W/"rev-N"`, body:
  ```json
  { "level": "project:qa-project", "revision": "rev_N", "values": { "chunking.max_tokens": 384 } }
  ```

**2) Editor đọc cùng level (phải bị từ chối)**

- Method: `GET`
- URL: `http://localhost:8000/v1/admin/settings?level=project:qa-project`
- Auth: Bearer `editor_token`
- Kỳ vọng: `403`

**3) Editor đọc level connection (được phép)**

- Method: `GET`
- URL: `http://localhost:8000/v1/admin/settings?level=connection:qa-project/qa-conn-a`
- Auth: Bearer `editor_token`
- Kỳ vọng: `200`

**4) Project không tồn tại**

- Method: `GET`
- URL: `http://localhost:8000/v1/admin/settings?level=project:nope`
- Auth: Bearer `admin_token`
- Kỳ vọng: `422`, `error.code = "reference_not_found"`

**5) Level sai định dạng**

- Method: `GET`
- URL: `http://localhost:8000/v1/admin/settings?level=planet:x`
- Auth: Bearer `admin_token`
- Kỳ vọng: `422`, `error.details = [{"field": "level", "issue": "..."}]`

**6) Không response nào chứa secret**

- Method: `GET`
- URL: `http://localhost:8000/v1/admin/settings?level=tenant`
- Auth: Bearer `admin_token`
- Kỳ vọng: `200`, search toàn bộ response body, không có chữ `secret`

---

## AC2 — Effective value và precedence

**1) connection qa-project/qa-conn-a → 256, origin=connection**

- Method: `GET`
- URL: `http://localhost:8000/v1/admin/settings?level=connection:qa-project/qa-conn-a&effective=true`
- Auth: Bearer `editor_token`
- Kỳ vọng: `values["chunking.max_tokens"] = {"value": 256, "origin": "connection"}`

**2) connection khác trong cùng project, chưa override riêng → 384, origin=project**

- Method: `GET`
- URL: `http://localhost:8000/v1/admin/settings?level=connection:qa-project/qa-conn-b&effective=true`
- Auth: Bearer `editor_token`
- Kỳ vọng: `values["chunking.max_tokens"] = {"value": 384, "origin": "project"}`

**3) project khác (chưa override) → 512, origin=tenant**

- Method: `GET`
- URL: `http://localhost:8000/v1/admin/settings?level=project:qa-project-2&effective=true`
- Auth: Bearer `admin_token`
- Kỳ vọng: `values["chunking.max_tokens"] = {"value": 512, "origin": "tenant"}`

**4) Key chưa set ở đâu cả → origin=default**

- Method: `GET`
- URL: `http://localhost:8000/v1/admin/settings?level=tenant&effective=true`
- Auth: Bearer `admin_token`
- Kỳ vọng: `values["ingestion.max_attempts"] = {"value": 3, "origin": "default"}`

---

## AC3 — Đổi 1 setting

**0) Lấy ETag hiện tại (chạy GET AC1 #1 ở trên, xem header response `ETag`,
vd `W/"rev-1"`)** — dùng giá trị này làm `If-Match` ở bước dưới.

**1) Patch project:qa-project**

- Method: `PATCH`
- URL: `http://localhost:8000/v1/admin/settings?level=project:qa-project`
- Auth: Bearer `admin_token`
- Headers: `If-Match: W/"rev-1"` (thay bằng ETag vừa lấy), `Content-Type: application/json`
- Body:
  ```json
  { "chunking.max_tokens": 300 }
  ```
- Kỳ vọng: `200`, header `ETag` tăng thêm 1 (vd `W/"rev-2"`), body:
  ```json
  {
    "revision": "rev_2",
    "dry_run": false,
    "changed": [
      { "key": "chunking.max_tokens", "from": 384, "to": 300, "applies": "next_ingestion" }
    ]
  }
  ```

**2) Đọc lại để xác nhận**

- Method: `GET`
- URL: `http://localhost:8000/v1/admin/settings?level=project:qa-project`
- Auth: Bearer `admin_token`
- Kỳ vọng: `values["chunking.max_tokens"] = 300`, ETag khớp revision mới

**3) Editor patch cùng request (phải bị từ chối)**

- Method: `PATCH`
- URL: `http://localhost:8000/v1/admin/settings?level=project:qa-project`
- Auth: Bearer `editor_token`
- Headers: `If-Match: W/"rev-2"`, `Content-Type: application/json`
- Body: `{ "chunking.max_tokens": 300 }`
- Kỳ vọng: `403`

> Lưu ý: `source`/`actor` của revision (`"api"` + `sub` của token) hiện **chưa
> expose qua HTTP** (để dành cho endpoint `GET /settings/revisions` ở v2.1) —
> chỉ lưu trong bảng `setting_scope_revisions`. Muốn xác nhận trực tiếp, query
> DB: `select tenant_id, scope_level, scope_id, revision, source, actor from
> setting_scope_revisions;`, hoặc chạy pytest
> `test_ac3_admin_patches_project_level_and_bumps_the_revision`.

---

## AC4 — Dry run và xoá override

**1) Dry run (không lưu gì cả)**

- Method: `PATCH`
- URL: `http://localhost:8000/v1/admin/settings?level=project:qa-project&dry_run=true`
- Auth: Bearer `admin_token`
- Headers: `If-Match: <ETag hiện tại>`, `Content-Type: application/json`
- Body: `{ "chunking.max_tokens": 300 }`
- Kỳ vọng: `200`, `dry_run: true`

**2) Đọc lại — giá trị và ETag KHÔNG đổi**

- Method: `GET`
- URL: `http://localhost:8000/v1/admin/settings?level=project:qa-project`
- Auth: Bearer `admin_token`

**3) Xoá override bằng null → fallback về tenant**

- Method: `PATCH`
- URL: `http://localhost:8000/v1/admin/settings?level=project:qa-project`
- Auth: Bearer `admin_token`
- Headers: `If-Match: <ETag hiện tại>`, `Content-Type: application/json`
- Body: `{ "chunking.max_tokens": null }`
- Kỳ vọng: `200`

**4) Đọc effective — phải fallback về tenant (512)**

- Method: `GET`
- URL: `http://localhost:8000/v1/admin/settings?level=project:qa-project&effective=true`
- Auth: Bearer `admin_token`
- Kỳ vọng: `values["chunking.max_tokens"] = {"value": 512, "origin": "tenant"}`

---

## AC5 — Các thay đổi bị từ chối

Mỗi request dưới đều `PATCH http://localhost:8000/v1/admin/settings?level=project:qa-project`
(trừ (g) dùng `level=tenant`), auth `admin_token`, `Content-Type: application/json`.
Lấy `If-Match` hiện tại trước khi chạy (trừ (e) cố ý bỏ, (f) cố ý dùng giá trị cũ).

**(a) Key sai chính tả → 422**

- Headers: `If-Match: <ETag hiện tại>`
- Body: `{ "chunking.max_tokenz": 1 }`

**(b) Giá trị vượt range → 422**

- Headers: `If-Match: <ETag hiện tại>`
- Body: `{ "chunking.max_tokens": 99999 }`

**(c) Key không cho phép ở level này → 422**

- Headers: `If-Match: <ETag hiện tại>`
- Body: `{ "ingestion.concurrency": 4 }`

**(d) Key hoàn toàn không tồn tại → 422**

- Headers: `If-Match: <ETag hiện tại>`
- Body: `{ "database.url": "postgres://..." }`

**(e) Thiếu If-Match → 422**

- Headers: _(không có `If-Match`)_
- Body: `{ "chunking.max_tokens": 300 }`

**(f) If-Match cũ (stale) → 412**

- Headers: `If-Match: W/"rev-0"`
- Body: `{ "chunking.max_tokens": 300 }`

**(g) Đổi model embedding → 409 reindex_required**

- URL: `http://localhost:8000/v1/admin/settings?level=tenant`
- Headers: `If-Match: <ETag hiện tại của tenant>`
- Body: `{ "models.embedding": "prov_openai:text-embedding-3-small" }`

**Kiểm tra cuối**: `GET` lại `level=project:qa-project` — `values`/`revision`
phải giống hệt trước khi chạy cả 7 case trên (không có gì bị lưu).

---

## AC6 — Seed lần đầu và phát hiện drift

AC6 là hành vi **start-up-only** (seed 1 lần từ `config/runtime_settings.yaml`
khi DB trống), không phải request HTTP nên **không test bằng Postman được**.
Test qua API thật đòi hỏi xoá sạch `setting_overrides`,
`setting_scope_revisions` và marker `settings_yaml_seed` trong
`workspace_settings` — **có tính phá hoại** nếu DB đó đang có dữ liệu thật
(DB dev hiện tại đang có dữ liệu từ các AC1-5 ở trên). Khuyến nghị dùng pytest
(đã cover đủ cả 3 vế của AC6):

```bash
uv run pytest packages/harborrag-runtime/tests/unit/test_settings_seed.py -v
```

Các test quan trọng:

- `test_seeds_empty_database_once` — seed lần đầu, revision ghi
  `source="seed"`.
- `test_second_boot_with_unchanged_file_does_not_reseed` — "restart" không
  seed lại, revision không tăng.
- `test_changed_file_logs_drift_without_reimporting` /
  `test_drift_log_names_the_key_that_changed` — sửa file sau khi đã seed ->
  log `WARN config_yaml_drift ... keys=chunking.max_tokens`, giá trị DB giữ
  nguyên (không áp dụng lại).

Nếu vẫn muốn test thủ công, **bắt buộc dùng 1 Postgres riêng cho việc này**
(không phải DB dev đang dùng ở AC1-5, vì bước dưới xoá sạch dữ liệu):

```sql
TRUNCATE setting_overrides, setting_scope_revisions;
UPDATE workspace_settings SET data = '{}'::json;
```

```bash
echo 'tenant: DEFAULT
settings:
  chunking.max_tokens: 512' > config/runtime_settings.yaml

# khởi động lại service (hoặc gọi trực tiếp CompositionRoot + seed_setting_overrides_once
# như trong packages/harborrag-app/src/harborrag_app/api/app.py::_seed_setting_overrides_once)

# -> effective value = 512, origin=tenant; revision record source="seed"

echo 'tenant: DEFAULT
settings:
  chunking.max_tokens: 1024' > config/runtime_settings.yaml

# restart lần nữa
# -> effective value VẪN là 512 (không re-seed); log có đúng 1 dòng WARN
#    config_yaml_drift ... keys=chunking.max_tokens; KHÔNG có revision mới
```

---

## Xử lý sự cố thường gặp

- **`404 { "code": "not_found" }` trên mọi route `/v1/admin/settings*`**:
  container API đang chạy build từ code cũ, chưa có route này. Rebuild +
  restart: `./scripts/deployment/dev.sh api --build` (mất vài phút, API gián
  đoạn ngắn trong lúc container được recreate).
- **`401 Unauthorized`, log server ghi `invalid token lifetime`**: token mint
  với `exp - iat` dài hơn `HARBORRAG_AUTH_MAX_TOKEN_LIFETIME_SECONDS` đang
  chạy thật (mặc định 3600s/1h nếu không set trong `env/.env.api`, dù code
  cho phép set tối đa 86400s/24h). Mint lại với lifetime ngắn hơn.
- **`422 reference_not_found`**: `project_id`/`connection_id` trong URL không
  tồn tại thật cho `tenant_id` của token (ví dụ token mint `tenants: ["qa"]`
  nhưng project/connection chưa được tạo bằng 4 request PUT ở mục "Dựng
  fixture"). Dựng lại fixture, hoặc kiểm tra dữ liệu hiện có bằng
  `GET /v1/admin/projects` (role `reader` trở lên, tenant của token).

---

## Ghi chú

- Tất cả AC1-AC6 đã có test tự động tương ứng trong:
  - `packages/harborrag-app/tests/test_api_admin_settings.py` (AC1-AC5, HTTP contract)
  - `packages/harborrag-app/tests/test_settings_service.py` (service-level)
  - `packages/harborrag-runtime/tests/unit/test_settings_seed.py` (AC6)
  - `packages/harborrag-adapters/tests/repositories/integration/test_setting_scope_revision_repository.py` (revision `source`/`actor`, chạy trên Postgres thật)
- **AC8 đã triển khai** (parser + chunking override áp dụng vào run thật):
  - **`settings_revision` thật trên mỗi run**: khi `POST /v1/admin/ingestions`
    (qua `connection_service.sync()`), hệ thống đọc effective settings của
    đúng connection scope (`SettingsService.read(effective=True)`, cùng logic
    cascade/revision của AC1-AC6) và gắn revision thật vào run — thay cho giá
    trị giả trước đây (`configuration_fingerprint` của connection, không liên
    quan gì đến settings). Xem `connection_sources.py:_resolve_connection_settings`,
    `connection_views.py:ingestion_view`.
  - **`parsing.pdf_parser` override thật**: khi connection scope có override
    (khác giá trị mặc định đăng ký), engine PDF bị ép buộc
    (`liteparse`/`docling`) cho mọi tài liệu của run tiếp theo, bỏ qua hoàn
    toàn chuỗi fallback theo profile. Cơ chế: `ParseRequest.engine` (đã có sẵn
    trong code nhưng chưa được dùng) + `PDFEngineRouter.override_engines` (mới,
    để engine bị ép có thể nằm ngoài `engine_order` của profile đang chạy mà
    không ảnh hưởng `.engines`/`.backends` — nhiều test cũ assert đúng danh
    sách này). Xem `harborrag_adapters/parsers/pdf/{parser,router}.py`,
    `harborrag_runtime/ingestion/document/capture.py`.
  - **`chunking.max_tokens` override thật**: khi connection scope có override,
    mọi chunk của run tiếp theo bị giới hạn đúng số token đó. Cơ chế:
    `ChunkingPlan` (đã có sẵn, nhưng trước đây không nơi nào trong pipeline
    thật truyền `plan` — luôn `None`) + `ChunkingService.plan_for_max_tokens()`
    (mới) — chỉ ép `hard_maximum_tokens`, các giới hạn còn lại (min/target/soft)
    lấy từ profile riêng của connector (vd Jira có ngưỡng thấp hơn Confluence)
    rồi kẹp xuống không vượt quá cap mới, để không làm sai lệch cấu hình
    chunking riêng của từng loại connector khi override chỉ nhắm vào 1 kết nối.
    Xem `harborrag_engine/ingestion/chunking/pipeline/candidates.py`,
    `harborrag_runtime/ingestion/document/materialization.py`.
  - Cả hai override **chỉ áp dụng khi thật sự bị override** (origin khác
    `"default"` ở bất kỳ cấp nào trong chain tenant→project→connection) — giá
    trị mặc định của registry không trùng với mặc định thật của từng loại
    connector (vd Jira chunking mặc định 1000 token, registry default 1100),
    nên không được áp dụng vô điều kiện.
  - Test mới: `test_connection_service.py` (2 test AC8 — revision thật +
    override thật reaching `PreparedSourceSubmission`),
    `test_parser_family_architecture.py` + `pdf/test_parser.py` (engine
    override, kể cả override nằm ngoài profile đang active),
    `test_plan_for_max_tokens.py` (chunking plan clamp + chặn token thật).
  - **Chưa làm** (AC9, nằm ngoài phạm vi AC8): Temporal activity options
    (`ingestion.concurrency`, `ingestion.activity_timeout`,
    `ingestion.max_attempts`) chưa được wire vào workflow, và
    `GET /v1/admin/failures` chưa tồn tại.

### Xác minh trực tiếp trên stack thật (Postgres + MinIO + Temporal)

Chạy `force` ingestion thật trên connection `qa/qa-project/qa-conn-a` (38 tài
liệu markdown) với `chunking.max_tokens=64` + `parsing.pdf_parser=docling`,
rồi đọc trực tiếp chunk artifact từ MinIO (`harborrag_qa/chunks/...jsonl`) và
so khớp `document_versions.chunk_artifact` trong Postgres. Phát hiện và sửa
2 lỗi thật mà unit test không bắt được (cả hai chỉ lộ ra khi chạy full-stack):

1. **`pdf_engine`/`chunking_max_tokens` bị rơi mất khi 1 run chia nhiều batch**:
   `SourcePlanRepository._dump()`/`_load()` (checkpoint giữa các batch của 1
   source scan dài) round-trip `DocumentReleaseRequest` qua JSON nhưng quên 2
   field mới — batch đầu tiên (giữ object in-memory) áp dụng override đúng,
   các batch sau (phải load lại từ checkpoint) lặng lẽ mất override. Sửa ở
   `harborrag_runtime/ingestion/source/plan.py` (`_dump`/`_load`).
2. **Force-ingestion không rechunk tài liệu đã tồn tại khi chỉ đổi settings**:
   `chunk_and_validate()`/`active_candidate()` coi tài liệu "đã chunk" là xong
   việc dựa trên `document_version_id` — vốn chỉ phụ thuộc nội dung, không
   phụ thuộc settings. Force lại 1 tài liệu *không đổi nội dung* vẫn tái dùng
   nguyên `chunk_artifact` cũ của lần ingest trước, bất kể `chunking.max_tokens`
   mới là gì. Sửa bằng cách gấp `pdf_engine`/`chunking_max_tokens` vào
   `ProcessingProfileInput.{parser_profile,chunk_strategy}` tại
   `prepare_source_submission()` (`harborrag_runtime/execution/source_submission.py`)
   — các field này đã là một phần của `processing_fingerprint` (dùng để quyết
   định NEW/UPDATED/FORCE_REPROCESS), nên override khác nhau giờ tạo
   `document_version_id` khác nhau một cách tự nhiên, kéo theo re-parse +
   re-chunk thật, đúng model admission hiện có (không phải case đặc biệt).
   Test hồi quy: `test_submission.py::test_pdf_engine_and_chunking_max_tokens_change_the_processing_identity`.

Sau 2 fix trên, xác minh lại bằng run thật: 38/38 tài liệu nhận
`document_version_id` hoàn toàn mới (0 trùng với run trước), 2234/2234
**evidence chunk** (loại dùng cho retrieval) đều ≤ 64 token; 185 **route
chunk** vượt 64 token nhưng đều ≤ 191 — đúng thiết kế vì route chunk có cap
riêng `ROUTE_MAXIMUM_TOKENS=512`, không dùng `chunking.max_tokens`.

`parsing.pdf_parser=docling` **không verify được bằng dữ liệu PDF thật** ở
stack dev hiện tại: `config/parsers.yaml` của deployment này chỉ enable
`liteparse` cho PDF (khối `pdf-docling` bị comment sẵn), và package `docling`
còn chưa được cài trong image worker. Request buộc dùng `docling` bị từ chối
đúng như thiết kế (`UnknownPDFEngineError`, giống lỗi gõ sai tên engine
trong YAML) — không phải bug, nhưng cũng không chứng minh được "docling
parse thật" trong môi trường này. Muốn verify đầy đủ cần một worker có
enable + cài `docling` trong `parsers.yaml`.
