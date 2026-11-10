# Test Acceptance Criteria cho Runtime Settings Overrides (AC1 - AC9)

Tài liệu này hướng dẫn cách test các acceptance criteria của tính năng runtime
setting overrides: AC1-AC6 (`setting_overrides`, `setting_scope_revisions`,
`/v1/admin/settings*`), AC8 (override áp dụng vào ingestion thật), AC9
(Temporal activity options) và AC10 (run đang chạy giữ nguyên settings). Mỗi
AC có 2 cách: chạy **pytest tự động** (khuyến nghị, nhanh, deterministic)
hoặc test **thủ công qua Postman** (copy method/URL/headers/body dưới đây vào
từng request) trên stack đang chạy.

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

## AC8 — Parsing và chunking áp dụng vào ingestion thật

**Đề bài**: Connection có override `parsing.pdf_parser` và/hoặc
`chunking.max_tokens` ở bất kỳ cấp nào trong chain tenant→project→connection.
Khi start 1 ingestion (đặc biệt là `force`) của connection đó, mọi tài liệu
của run phải dùng đúng engine PDF / giới hạn token đã override — không phải
giá trị mặc định của registry, và không phải tái dùng artifact cũ nếu chỉ có
settings đổi (nội dung tài liệu không đổi). Run cũng phải mang theo
`settings_revision` thật của đúng connection scope đó.

### Cách 1 — pytest (không cần stack chạy)

```bash
uv run pytest packages/harborrag-app/tests/test_connection_service.py -k ac8 -v
uv run pytest packages/harborrag-adapters/tests/parsers/unit/test_parser_family_architecture.py -v
uv run pytest packages/harborrag-adapters/tests/parsers/unit/pdf/test_parser.py -v
uv run pytest packages/harborrag-engine/tests/ingestion/unit/test_plan_for_max_tokens.py -v
uv run pytest packages/harborrag-runtime/tests/runtime_ingestion/unit/source/test_plan.py -v
uv run pytest packages/harborrag-runtime/tests/runtime_ingestion/unit/temporal/test_submission.py -v
```

### Cách 2 — test thủ công bằng Postman (trên stack thật)

Dùng lại fixture `qa-project`/`qa-conn-a` đã dựng ở mục "0. Chuẩn bị".

**1) Override PDF parser + chunking ở connection scope**

- Method: `PATCH`
- URL: `http://localhost:8000/v1/admin/settings?level=connection:qa-project/qa-conn-a`
- Auth: Bearer `admin_token`
- Headers: `If-Match: <ETag hiện tại của scope này>`, `Content-Type: application/json`
- Body:
  ```json
  { "parsing.pdf_parser": "liteparse", "chunking.max_tokens": 64 }
  ```
- Kỳ vọng: `200`, `changed` liệt kê cả 2 key

**2) Start force ingestion của connection**

- Method: `POST`
- URL: `http://localhost:8000/v1/admin/ingestions`
- Auth: Bearer `editor_token`
- Headers: `Content-Type: application/json`
- Body:
  ```json
  { "project_id": "qa-project", "connection_id": "qa-conn-a", "mode": "force" }
  ```
- Kỳ vọng: `202`, header `Location` trỏ tới `GET /v1/admin/ingestions/{ingestion_id}`

**3) Đọc lại run, kiểm tra `settings_revision` thật**

- Method: `GET`
- URL: `http://localhost:8000/v1/admin/ingestions/{ingestion_id}`
- Auth: Bearer `reader_token`
- Kỳ vọng: `settings_revision` khớp đúng revision hiện tại của
  `connection:qa-project/qa-conn-a` (không phải `configuration_fingerprint`)

**4) Chờ run `COMPLETED`, kiểm tra chunk thật** — xem chi tiết cách đọc chunk
artifact trực tiếp từ MinIO + so khớp Postgres ở mục "Xác minh trực tiếp trên
stack thật" ngay dưới đây; cách nhanh hơn là dùng pytest ở Cách 1.

### Cơ chế

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

**File đã sửa**: `harborrag_runtime/ingestion_contracts.py`,
`harborrag_runtime/execution/{source_submission,gateway}.py`,
`harborrag_runtime/execution/source_conversion.py`,
`harborrag_runtime/ingestion/source/{models,tasks,plan,descriptor_mapping}.py`,
`harborrag_runtime/ingestion/document/{models,capture,materialization}.py`,
`harborrag_adapters/parsers/pdf/{router,parser}.py`,
`harborrag_adapters/parsers/factory.py`, `harborrag_engine/ingestion/base.py`,
`harborrag_engine/ingestion/chunking/pipeline/{candidates,service}.py`,
`harborrag_app/workflow_control/control_plane/connection_views.py`,
`harborrag_app/workflow_control/ingestion/connection_sources.py`.

**Test mới**: `test_connection_service.py` (2 test AC8 — revision thật +
override thật reaching `PreparedSourceSubmission`), `test_parser_family_architecture.py`
+ `pdf/test_parser.py` (engine override, kể cả override nằm ngoài profile
đang active), `test_plan_for_max_tokens.py` (chunking plan clamp + chặn token
thật), `test_plan.py` (bug fix: checkpoint JSON không làm rơi mất override
giữa các batch), `test_submission.py` (bug fix: override được gấp vào
`processing_fingerprint` để force-reprocess thật sự rechunk).

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
- **AC8 đã triển khai** — xem mục "AC8" bên dưới.
- **AC9 đã triển khai đầy đủ (Temporal wiring + `GET /v1/admin/failures`),
  AC10 đã có test riêng** — xem mục "AC9" bên dưới.

---

## AC9 — Temporal settings reach the workflow

**Đề bài**: Connection `cpm/cpm-tickets` có 1 tài liệu luôn fail khi parse.
`PATCH ?level=connection:cpm/cpm-tickets` với
`{"ingestion.concurrency": 2, "ingestion.activity_timeout": "PT2M",
"ingestion.max_attempts": 2}`, rồi start force ingestion. Kỳ vọng: Temporal
history của run cho thấy timeout start-to-close 2 phút và retry policy 2
attempt trên document activity, tối đa 2 document chạy cùng lúc; tài liệu
fail xuất hiện ở `GET /v1/admin/failures?connection=cpm/cpm-tickets` với
`attempts = 2`.

### Phần 1 — Wiring 3 setting vào Temporal workflow (đã xong)

**Vấn đề gốc phát hiện được**: `IngestionTemporalClient.start()`
(`harborrag_runtime/temporal/client.py`) luôn ghi đè toàn bộ
`workflow_options` của request bằng config tĩnh của deployment
(`self._config.workflow_options()`) trước khi start workflow — nghĩa là dù
có resolve được override theo connection ở tầng trên, nó vẫn bị vứt bỏ hoàn
toàn ngay trước khi gửi cho Temporal. Đây là lý do chính khiến AC9 không thể
"chỉ thêm field là xong" như AC8 — phải sửa đúng chỗ `client.start()` này.

**Cơ chế** (theo đúng pattern đã dùng ở AC8: resolve 1 lần lúc submit, bake
cứng vào input, workflow không bao giờ đọc lại settings giữa chừng):

- **`ingestion.concurrency`** → map thẳng vào field `document_concurrency`
  đã có sẵn trên `PreparedSourceSubmission`/`SourceIngestionInput` — field
  này đã được `source_batch_workflow.py` dùng sẵn để giới hạn số document
  activity chạy đồng thời trong 1 batch, nên **không cần thêm wiring Temporal
  mới**, chỉ cần dẫn setting vào đúng chỗ resolve.
- **`ingestion.activity_timeout`** (chuỗi ISO 8601, vd `"PT2M"`) và
  **`ingestion.max_attempts`** → 2 field mới trên `PreparedSourceSubmission`:
  `ingestion_activity_timeout_seconds`, `ingestion_max_attempts` (giống hệt
  cách AC8 thêm `pdf_engine`/`chunking_max_tokens`). `IngestionTemporalClient`
  được thêm hàm `_resolved_workflow_options()`: lấy `workflow_options` nền từ
  config deployment rồi **overlay** 2 giá trị override này lên trên (thay vì
  ghi đè toàn bộ như cũ), trước khi start workflow.
  - `ActivityTimeoutConfig` (trong `temporal_models.py`) có thêm field
    `document_seconds` (mặc định 1800s = giá trị hardcode cũ); 2 activity
    `fetch_and_capture_raw` và `parse_and_normalize` trong
    `document_workflow.py` (nơi việc parse PDF thật sự chạy) đổi từ
    `timedelta(minutes=30)` hardcode sang đọc field này.
  - Chuỗi ISO 8601 duration được parse bằng hàm mới
    `harborrag_runtime/ingestion/durations.py::parse_iso8601_duration_seconds()`
    (hỗ trợ `PnDTnHnMnS`, không cần thêm dependency ngoài).
- **`ConnectionSourceInputBuilder._resolve_connection_settings()`**
  (`harborrag_app/workflow_control/ingestion/connection_sources.py`) được mở
  rộng để đọc thêm 3 key `ingestion.concurrency` / `ingestion.activity_timeout`
  / `ingestion.max_attempts` qua `SettingsService.read(effective=True)`, theo
  đúng luật đã áp dụng cho AC8: **chỉ áp dụng khi origin khác `"default"`**
  (connection/project/tenant thật sự có set override), không áp dụng vô điều
  kiện giá trị mặc định của registry.

**File đã sửa**:
- `packages/harborrag-runtime/src/harborrag_runtime/temporal_models.py`
- `packages/harborrag-runtime/src/harborrag_runtime/temporal/document_workflow.py`
- `packages/harborrag-runtime/src/harborrag_runtime/temporal/client.py`
- `packages/harborrag-runtime/src/harborrag_runtime/ingestion_contracts.py`
- `packages/harborrag-runtime/src/harborrag_runtime/execution/source_submission.py`
- `packages/harborrag-runtime/src/harborrag_runtime/execution/gateway.py`
- `packages/harborrag-app/src/harborrag_app/workflow_control/ingestion/connection_sources.py`
- Mới: `packages/harborrag-runtime/src/harborrag_runtime/ingestion/durations.py`

**Vì sao AC10 ("running ingestion giữ nguyên settings") tự động đúng cho cả
3 setting này**: `workflow_options` (đã bao gồm 2 override mới) và
`document_concurrency` đều là field của `SourceIngestionInput` — input này
chỉ được build **một lần** lúc `POST /v1/admin/ingestions`/`start()`, rồi
bake cứng vào Temporal workflow history; mọi lần workflow continue-as-new
hay dispatch batch tiếp theo đều truyền lại nguyên `request.workflow_options`
đã có (xem `source_workflow.py`, `source_batch_workflow.py`) — **không có
chỗ nào đọc lại DB settings giữa chừng 1 run**. PATCH sau đó chỉ ảnh hưởng
tới run **tiếp theo** (lần `prepare_source_submission()` kế tiếp sẽ đọc được
revision/override mới).

**Test đã chạy**: 127 test runtime liên quan (temporal config/client/
document_workflow/submission) pass; mypy clean trên toàn bộ file đã sửa; full
suite `harborrag-runtime` (1417 pass) và `harborrag-app` (1029 pass) — các
test fail còn lại (migration count, timestamp, path-outside-root trên
Windows, scaffold/init CLI) đã xác nhận **có từ trước**, không liên quan tới
thay đổi này (confirm bằng `git stash` rồi chạy lại cùng test).

### Test đã viết cho AC9 (phần 1) + AC10

```bash
uv run pytest packages/harborrag-app/tests/test_connection_service.py -k "ac9 or ac10" -v
uv run pytest packages/harborrag-runtime/tests/runtime_ingestion/unit/temporal/test_client.py -k "overlay or deployment_defaults" -v
```

- `test_ac9_ingestion_concurrency_timeout_and_max_attempts_reach_the_prepared_source`
  — override cả 3 key ở connection scope, xác nhận `ConnectionSourceInputBuilder`
  resolve đúng (`ingestion.activity_timeout="PT2M"` → 120 giây).
- `test_ac9_unset_ingestion_settings_stay_none` — không override gì → cả 3
  field resolve về `None` (dùng default của worker/deployment, không phải
  default của registry).
- `test_client_overlays_a_connections_activity_timeout_and_max_attempts` —
  ở tầng `IngestionTemporalClient.start()`: request có
  `ingestion_activity_timeout_seconds=120`/`ingestion_max_attempts=2` thì
  `workflow_options` được submit lên Temporal có đúng
  `timeouts.document_seconds=120`, `retries.document.maximum_attempts=2`,
  **đồng thời `task_queues` và `timeouts.discovery_seconds` vẫn giữ nguyên
  theo config deployment** (overlay, không ghi đè toàn bộ).
- `test_client_leaves_workflow_options_at_deployment_defaults_without_overrides`
  — request không có override nào → `workflow_options` y hệt
  `TemporalRuntimeConfig.workflow_options()` của deployment (không bị đổi bởi
  cơ chế overlay mới).
- **`test_ac10_a_running_ingestions_resolved_settings_survive_a_later_patch`**
  — test trực tiếp kịch bản AC10: build input cho 1 "run đang chạy" với
  `chunking.max_tokens=512`/`ingestion.concurrency=4` (revision 1); PATCH
  connection scope xuống `256`/`2` (revision 2) **sau khi** run đó đã resolve;
  xác nhận object đã capture của run cũ **không đổi** (`settings_revision=1`,
  `512`, `4`), rồi build input cho "run kế tiếp" và xác nhận nó thấy đúng
  `settings_revision=2`, `256`, `2`. Test này không cần Temporal thật — nó
  verify đúng chỗ kiến trúc đảm bảo AC10 đúng: settings chỉ resolve 1 lần lúc
  submit, object trả về là frozen dataclass, PATCH sau đó không thể và không
  hề chạm vào object đã trả về trước đó.

Tất cả pass (37/37 `test_connection_service.py`, 28/28
`temporal/test_client.py`), mypy clean trên code nguồn; các file test có vài
warning mypy tiền tồn tại (pattern `capture(*args, **kwargs)` không khớp
chữ ký `Callable[..., PreparedSourceSubmission]`, và fake SDK client không
khớp type `Client` thật) — đã xác nhận bằng `git stash` là có từ trước, các
test mới chỉ lặp lại đúng pattern cũ, không phải lỗi mới.

### Phần 2 — `GET /v1/admin/failures?connection=...` (đã xong)

**Cơ chế**: bảng `document_failures` chỉ có `document_id`/
`document_version_id`, không có `connection_id`/`tenant_id` trực tiếp — join
sang bảng `documents` (có sẵn `tenant_id`, `connection_id` dạng
`project_id/connection_id`, ví dụ `"cpm/cpm-tickets"`, đúng format
`candidate.source_identity.connection_id` ghi vào khi publish document) để
lọc đúng connection. Luồng mới, đi theo đúng pattern đã có của
`list_documents`/`list_tasks`:

- `IngestionReliabilityRepository.list_failures(tenant_id, connection_id, limit)`
  (adapters) — `SELECT ... JOIN documents ... WHERE tenant_id = ? AND
  connection_id = ? ORDER BY last_failure_at DESC`, map qua
  `document_failure_record_from_row()` thành `DocumentFailureRecord` (model
  đọc mới trong `harborrag_core.ingestion`, tách khỏi `DocumentFailure` —
  model ghi — vì shape khác nhau: có `attempt_count`/timestamps, không có
  `artifact_references`).
- `IngestionTaskRegistry.list_failures()` (runtime) — uỷ quyền thẳng xuống
  `control.reliability.list_failures()`.
- `PublicTaskStore` Protocol (`harborrag_app/workflow_control/ingestion/ports.py`)
  thêm chữ ký `list_failures` — `TaskRegistry` Protocol (composition/factories.py)
  tự động thoả mãn vì nó extend `PublicTaskStore`.
- `FailuresService` (mới, `harborrag_app/workflow_control/ingestion/failures.py`)
  — use case mỏng gọi `task_registry()` rồi `store.list_failures(...)`, theo
  đúng pattern `DurableIngestionOperations`/`IngestionApplicationService` đã
  dùng cho `task_registry` provider.
  `AppService.failures_service` (property mới) expose nó; thêm vào Protocol
  `ConnectionAdministration` để route có thể gọi qua `Administration`
  dependency (giống `connection_service`/`settings_service`).
- Route mới `GET /v1/admin/failures` (`harborrag_app/api/v1/admin/failures_routes.py`),
  role tối thiểu `reader`, tenant lấy từ `TenantDependency` (giống
  `GET /v1/admin/settings`). Query bắt buộc `?connection={project_id}/{connection_id}`,
  validate lại bằng chính `parse_level("connection:" + value)` đã dùng cho
  `/v1/admin/settings` (cùng format, cùng thông báo lỗi 422 khi sai). Trả
  `{"items": [{document_id, document_version_id, failed_stage, category,
  retryable, safe_error_code, attempts, first_failure_at, last_failure_at}]}`
  — `attempts` chính là `attempt_count` của bảng.
- Đăng ký router mới vào `harborrag_app/api/router.py` (cùng chỗ
  `connection_admin_routers`/`settings_admin_routers`).

**File mới**:
- `packages/harborrag-app/src/harborrag_app/api/v1/admin/failures_routes.py`
- `packages/harborrag-app/src/harborrag_app/api/v1/admin/failures_schemas.py`
- `packages/harborrag-app/src/harborrag_app/workflow_control/ingestion/failures.py`

**File đã sửa**:
`harborrag_core/ingestion/{lifecycle_contracts,__init__}.py` (model đọc mới
`DocumentFailureRecord`),
`harborrag_adapters/repositories/database/ingestion_control/{reliability,row_mapping}.py`,
`harborrag_runtime/execution/task_registry.py`,
`harborrag_app/workflow_control/ingestion/ports.py`,
`harborrag_app/workflow_control/composition/service.py`,
`harborrag_app/workflow_control/control_plane/administration.py`,
`harborrag_app/api/router.py`.

### Cách test

```bash
# Repository (join tenant+connection, cách ly tenant/connection khác) — sqlite thật
uv run pytest packages/harborrag-adapters/tests/repositories/unit/test_ingestion_control_reliability.py -k list_failures -v

# Service (wiring tenant/connection/limit xuống store) — fake store
uv run pytest packages/harborrag-app/tests/test_failures_service.py -v

# Route tồn tại đúng chỗ, khớp OpenAPI surface
uv run pytest packages/harborrag-app/tests/test_export_openapi.py -v
```

Postman (reader_token trở lên, đã dựng fixture ở mục "0. Chuẩn bị"):

- Method: `GET`
- URL: `http://localhost:8000/v1/admin/failures?connection=qa-project/qa-conn-a`
- Auth: Bearer `reader_token`
- Kỳ vọng: `200`, `items: []` nếu connection chưa có tài liệu fail nào; sau
  khi force 1 tài liệu luôn fail parse (vd input hỏng) rồi chờ run xong,
  `items` có đúng tài liệu đó với `attempts` tăng dần mỗi lần retry.

**Test đã chạy**: 20/20 pass (`test_ingestion_control_reliability.py` +
`test_failures_service.py`), 5/5 pass (`test_export_openapi.py`, đã cập nhật
golden route set — nhân tiện cũng thêm lại 2 path của `/v1/admin/settings*`
vốn đã thiếu từ AC1-6, không liên quan AC9 nhưng phát hiện ra khi sửa), mypy
clean trên toàn bộ file nguồn mới/sửa. Route tự xác nhận đã đăng ký đúng qua
`app.openapi()["paths"]`. `test_api_access.py` có 2 case fail tiền tồn tại
(route `/v1/admin/settings` thiếu `settings_service` trên fake test
`_AccessApp`, không liên quan AC9) — xác nhận route `/v1/admin/failures` MỚI
không thêm fail nào vào test này, vì `?connection=` là query bắt buộc nên
request thiếu nó bị FastAPI chặn ở tầng validation (422) trước khi chạm vào
handler/`failures_service`.
