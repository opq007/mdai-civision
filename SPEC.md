# mdai-civision — ModelScope Civision 视频生成 OpenAI 兼容网关

> 把魔搭社区海外版（ModelScope Civision）的**网页端视频生成**能力，包装成一个可独立调用、符合 OpenAI Videos API 标准的接口，并附带多账号 / 代理 / 魔粒额度管理后台。

本文件是需求澄清后的**确认规格**。所有标注「实测」的结论都来自对 `www.modelscope.ai` 前端产物的直接逆向；标注「待验证」的部分需要真实账号确认。

---

## 1. 已确认的技术事实（逆向结果）

### 1.1 关键前提：公开 API 不覆盖视频，必须走网页内部接口

ModelScope 对外开放的官方 API（`api-inference.modelscope.cn`）只提供 LLM 与文生图，**没有视频模型**。视频生成只存在于网页端，走的是同源的 Cookie 会话接口。本项目即封装这一层。

### 1.2 两条视频生成通路（均已定位）

#### 通路 A —— 经典 Muse 通路（**已实测跑通，选用**）

| 用途 | 方法 | 路径 |
|---|---|---|
| 提交任务 | `POST` | `/api/v1/muse/predict/task/video/submit` |
| 轮询状态 | `GET` | `/api/v1/muse/predict/task/video/status?taskId=<id>` |
| 结果详情 | `GET` | `/api/v1/muse/predict/queryVideoPredictDetail?...` |
| 魔粒预估 | `POST` | `/openapi/v1/aigc/agentic/estimate-quota`（snake_case） |
| 历史列表 | `POST` | `/api/v1/muse/predict/queryVideoHistoryList` |
| 图片下载 URL | `GET` | `/api/v1/muse/image/getImageDownloadUrl` |

- 提交返回 `Data.data.taskId`（实测为字符串，如 `"33090"`）
- **实测状态取值**：`PROCESSING` → `UNDER_REVIEW` → `SUCCEED`（另有 `FAILED` / `CANCELED`）
  > 注意与前端源码中的 `PENDING` / `RUNNING` 不一致，**以实测为准**
- 状态响应字段：`status` / `progress{percent,detail}` / `url` / `predictResult` / `imageId` / `errorCode` / `errorMsg`
- `SUCCEED` 时 `url` 直接给出成品视频地址（实测形如
  `https://muse-intl.oss-ap-southeast-1.aliyuncs.com/video/<uuid>.mp4`）
- 轮询间隔：**5000 ms**（实测前端常量 `Pa = 5e3`；实测 4s 480×480 任务约耗时 100s）
- 请求体（实测可用）：
  ```json
  {
    "predictType": "TXT_2_VIDEO",
    "promptArgs": { "prompt": "...", "negativePrompt": "" },
    "basicDiffusionArgs": { "width": 480, "height": 480, "totalSeconds": 4,
                            "guidanceScale": 7.5, "numInferenceSteps": 50, "seed": -1 },
    "modelArgs": { "checkpointModelVersionId": 546, "loraArgs": [] }
  }
  ```
  - ⚠️ `videoInferenceArgs` **仅 MiniMax-H3 支持**，其他模型带上会报
    `"videoInferenceArgs 仅支持 MiniMax-H3"`
  - ⚠️ 提交有**频率限制**，过快返回 `code: 50001` `"Request frequency too fast"`（实测需间隔 ~40s）

**实测成功案例**（4s / 480×480 / Wan2.1-T2V-1.3B）：
```
submit  → taskId 33090
poll    → PROCESSING ×15 → UNDER_REVIEW ×6 → SUCCEED
url     → https://muse-intl.oss-ap-southeast-1.aliyuncs.com/video/aec8714e-...mp4
balance → 300 → 298 魔粒（消耗 2）
file    → 118781 bytes, ftypisom（合法 MP4）
estimate→ quota: 2 魔粒（与实际扣减一致）
```

#### 通路 B —— Agentic 通路（已定位，备用）

| 用途 | 方法 | 路径 |
|---|---|---|
| 生成 | `POST` | `/openapi/v1/aigc/agentic/video/generate` |
| 配额预估 | `POST` | `/openapi/v1/aigc/agentic/estimate-quota` |
| 轮询 | `GET` | `/api/v1/aigc/agentic/messages/<id>/tasks/status` |
| 对话 | `POST` | `/api/v1/aigc/agentic/conversations` |

请求体使用 **snake_case**（`video_predict_task_request.predict_type`、`basic_diffusion_args`、`model_args.checkpoint_model_version_id`），字段名与通路 A 不同，需单独适配层。

> **决策**：以**通路 A 为主实现**（已实测跑通，字段 camelCase、结构清晰、状态机明确），通路 B 作为可选后端，预留 adapter 接口。
> 注意两路的**魔粒预估端点共用** `/openapi/v1/aigc/agentic/estimate-quota`，但其请求体是 **snake_case**（见下）。

### 1.3 鉴权模型（关键约束）

**会话是 Cookie 形态，且作用域为 `modelscope.ai` 主机。**

实测 Server 下发的 Cookie：

| Cookie | 用途 | HttpOnly | 有效期 |
|---|---|---|---|
| `csrf_session` | 会话主体 | 否 | 30 天 |
| `csrf_token` | CSRF 令牌 | 否 | 30 天 |
| `acw_tc` | 阿里云 WAF | 是 | 1800s |
| `auth_sid` | OAuth 中转会话 | 是 | 900s |
| **`m_session_id`** | **登录态主体** | 是 | 30 天 |
| **`h_uid`** | **登录态主体** | 是 | 30 天 |

> ⚠️ **重要**：`csrf_session` / `csrf_token` **匿名访问也会下发**，仅凭它们存在**不能**判定已登录。
> 真正的登录态由 **`m_session_id` + `h_uid`** 承载（实测：只带 csrf 系列的 cookie 调用接口一律 401；
> 带上完整集合后 `/api/v1/users/info` 与 `/openapi/v1/magicubes/balance` 均返回 200）。

**CSRF 契约**（实测前端拦截器）：对 `POST` / `PUT` / `DELETE` 请求，必须附带请求头 `X-CSRF-TOKEN`，值是 `csrf_token` Cookie **URL 解码后**的值；`GET` 不需要。另有可选的 `X-XSRF-TOKEN`（若存在 `xsrf` Cookie）。

**失败模式的可区分性**（实测，不同路径的报错文案不同，说明是两层鉴权）：

| 路径 | 未登录时的响应 |
|---|---|
| `/api/v1/*` | `{"Code":10010101003,"Message":"user not logged in"}` |
| `/openapi/v1/magicubes/*` | `{"code":"InvalidAuthentication","message":"authentication required"}` |
| `/openapi/v1/aigc/agentic/*` | `{"code":"InvalidAuthentication","message":"user not logged in"}` |

> ✅ **已验证**：Cookie 会话**可以**在纯 HTTP 客户端（httpx）中完整复现，无需浏览器常驻。
> 服务端只需持久化这组 Cookie 即可长期使用。

### 1.4 OAuth（GitHub）

**实测确认可用**，且**支持跨机器**。

```
GET /api/v1/auth/login/generate/oauth2/url
    ?callback_url=<我们的回调地址>
    &provider=github
→ { data: { github_auth_url: "https://www.modelscope.ai/api/v1/auth/login/oauth2/to/github?request_id=...&callback_url=...&state_token=..." } }
```

- `provider=github` 是选择 provider 的**唯一正确参数名**（`type` / `oauth_type` / `login_type` / `source` / `platform` 均无效）
- 不传 `provider` 时返回 `google_auth_url`
- `callback_url` **接受任意地址**，包括 `http://127.0.0.1:8899/cb`（实测）
- GitHub 侧 client_id：`Ov23ligFgeAx1Jfufl85`，scope `read:user`，redirect_uri 固定为 `https://www.modelscope.ai/api/v1/auth/login/oauth2/code/github`

**实测回调行为**（`tools/oauth_capture.py` 抓包）：回调只携带 `?request_id=<uuid>`，**不带任何 Cookie 或 token**。

> ⚠️ **待验证（已部分解答）**：会话未随回调交付 → ModelScope 的会话 Cookie 是 host-only（`modelscope.ai`），跨域的我们的服务端读不到。
> **已验证**：不存在可凭 `request_id` 换取会话的服务端接口。已探测
> `/api/v1/auth/login/oauth2/{session,ticket,token,status,result,poll,check,exchange,user,info}`
> —— 全部返回与伪造路径**完全相同的 302 → /login**（已用 baseline 严格比对确认）。
>
> **已验证的落地方式**：用 Playwright 驱动**持久化 profile** 完成 OAuth，随后
> **把完整 Cookie 集合导出到服务端**，之后服务端用纯 httpx 即可正常工作（无需浏览器常驻）。
> 会话主体是 `m_session_id` + `h_uid`（见 §1.3）。
>
> **由此得出的实现结论**：浏览器仅用于**首次授权与凭据采集**；运行期是纯 HTTP 反代。

### 1.5 视频模型目录（实测，8 个）

来源：`GET /api/v1/muse/predict/unauth/defaultTemplateV2` → `data.VIDEO.supportSDVersionList`

| civision 值 | 展示名 | 类型 | 支持 predictType | versionId |
|---|---|---|---|---|
| `WAN_VIDEO_2_1_T2V_1_3_B` | Wan2.1-T2V-1.3B | T2V | TXT_2_VIDEO | 546 |
| `WAN_VIDEO_2_1_T2V_14_B` | Wan2.1-T2V-14B | T2V | TXT_2_VIDEO | 650 |
| `WAN_VIDEO_2_1_I2V_14_B` | Wan2.1-I2V-14B-720P | I2V | IMG_2_VIDEO | 648 |
| `WAN_VIDEO_2_1_FLF2V_14_B` | Wan2.1-FLF2V-14B-720P | FLF2V | FLF_2_VIDEO | 649 |
| `WAN_VIDEO_2_2_TI2V_5_B` | Wan2.2-TI2V-5B | IT2V | TXT_2_VIDEO, IMG_2_VIDEO | 626 |
| `WAN_VIDEO_2_2_T2V_A_14_B` | Wan2.2-T2V-A14B | T2V | TXT_2_VIDEO | 557 |
| `WAN_VIDEO_2_2_I2V_A_14_B` | Wan2.2-I2V-A14B | I2V | IMG_2_VIDEO | 647 |
| `MINIMAX_H3` | MiniMax-H3 | I2V | TXT_2_VIDEO, IMG_2_VIDEO, FLF_2_VIDEO | 150867 |

每模型自带 `videoSizeSlider`：

- `large`（720 档）：9:16 = 720×1280、3:4 = 720×960、1:1 = 720×720、4:3 = 960×720、16:9 = 1280×720
- `small`（480 档）：9:16 = 480×848、3:4 = 480×640、1:1 = 480×480、4:3 = 640×480、16:9 = 848×480

默认参数（实测）：`guidanceScale` 7.5（Wan）/ 4.0（MiniMax-H3）、`numInferenceSteps` 50（Wan）/ 20（H3）、`totalSeconds` 3（默认值，实际由 UI 调整）、`seed` -1。

> 注：目录是**实时**的，不硬编码 —— 后台按上述接口拉取并缓存。

### 1.6 魔粒（MagicCube）

| 用途 | 方法 | 路径 |
|---|---|---|
| 余额 | `GET` | `/openapi/v1/magicubes/balance` → `{ total_balance, available_balance, frozen_amount }` |
| 消费费率 | `GET` | `/openapi/v1/magicubes/spend/rates` |
| 交易流水 | `GET` | `/openapi/v1/magicubes/transactions?type=&category_key=&page=&page_size=` |
| 赚取规则 | `GET` | `/openapi/v1/magicubes/earn/rules` |

**实测费率表**（共 14 条，与视频相关的只有一条）：

```json
{ "scene": "aigc_video", "model_tier": "standard", "unit": "request",
  "unit_price": 1, "min_charge": 1, "description": "主流模型 1魔粒每次" }
```

**实测魔粒预估**（`POST /openapi/v1/aigc/agentic/estimate-quota`）：

```json
{ "type": "VIDEO", "mode": "custom", "video_predict_task_request": {
    "predict_type": "TXT_2_VIDEO",
    "prompt_args": { "prompt": "..." },
    "basic_diffusion_args": { "width":480, "height":480, "total_seconds":4,
                              "guidance_scale":7.5, "num_inference_steps":50, "seed":-1 },
    "model_args": { "checkpoint_model_version_id": 546 } } }
→ { "success": true, "data": { "quota": 2 } }
```

⚠️ 该端点的字段是 **snake_case**（与通路 A 的 camel_case 提交体不同），且：
- `mode` 只接受 `custom`（`agent` 会报"暂不支持"）
- 必须用 `video_predict_task_request`（驼峰写法报"视频预估需要传入 video_predict_task_request"）

**实测扣费**：4s / 480×480 / Wan2.1-T2V-1.3B → 预估 2 魔粒，实际余额 300 → 298，**完全一致**。
可见消耗并非固定 1 魔粒/次，而与**时长 / 分辨率**相关（4s 低分辨率 = 2 魔粒）。

### 1.7 OpenAI 目标契约（取自官方 OpenAPI spec v2.3.0）

```
POST /v1/videos                       → 200 VideoResource
GET  /v1/videos/{video_id}            → 200 VideoResource
GET  /v1/videos/{video_id}/content    → 200 video/mp4 (binary)
GET  /v1/videos                       → 列表
DELETE /v1/videos/{video_id}
```

`VideoResource` 必填字段：
`id, object("video"), model, status, progress, created_at, completed_at, expires_at, prompt, size, seconds, remixed_from_video_id, error`

- `status` 枚举：`queued` | `in_progress` | `completed` | `failed`
- `model` 枚举：`sora-2` | `sora-2-pro` | `sora-2-2025-10-06` | `sora-2-pro-2025-10-06` | `sora-2-2025-12-08`
- `seconds` 枚举：`"4"` | `"8"` | `"12"`
- `size` 枚举：`720x1280` | `1280x720` | `1024x1792` | `1792x1024`
- 请求体：`model` / `prompt`(1..32000) / `input_reference{image_url|file_id}` / `seconds` / `size`；支持 `multipart/form-data` 与 `application/json`
- `input_reference.image_url` 支持完整 URL 或 base64 data URL

### 1.7 实测补充：魔粒结算与耗时

| 组合 | 预估 | 实际扣减 | 耗时 |
|---|---|---|---|
| 4s / 480×480 / Wan2.1-T2V-1.3B（versionId 546） | 2 | 2 | ~100 s |
| 4s / 720×1280 / Wan2.2-T2V-A14B（versionId 557） | 23 | 23 | **> 25 分钟** |

**MiniMax-H3 的「LoRA 加速」开关（关键）**

civision 页面为 MiniMax-H3 提供一个 *LoRA acceleration* 开关，
**默认打开**。打开时：

- `numInferenceSteps` 被**强制为 4**（前端 `C && f.setFieldValue(va.numInferenceSteps, 4)`），
  且采样步数滑块被隐藏（`!C && ...`）
- 该开关只对 MiniMax-H3 出现（前端 `x = p === 'MINIMAX_H3'`）
- **魔粒消耗减半**

因此若不发送 `videoInferenceArgs.accelerateLora = true`，
估算会按 MiniMax-H3 的**发布默认步数 20** 计算，**得到约 2 倍**的价格。

实测对照（768×1280）：

| 时长 | 页面显示 | 服务（修正前） | 服务（修正后） |
|---|---|---|---|
| 3s | 34 | 68 | **34** |
| 5s | 56 | 113 | **56** |
| 10s | 113 | 225 | **113** |

> 估算端点与提交端点使用**同一个 `video_predict_task_request` 结构**（仅字段命名不同），
> 因此可以用免费的 `estimate-quota` 端点验证请求体是否被上游接受 —— 无需真正发起生成。

**预估与实际扣减必须一致的前提**：估算请求体的每个参数都要与提交请求体一致
（尺寸、时长、步数、引导系数、加速开关）。此前估算端点把
`guidance_scale` / `num_inference_steps` 写死为 7.5 / 50，
与实际提交不符，导致预估失真。

> **魔粒消耗由分辨率主导**，不是费率表里写的固定「1 魔粒每次」（那是起步价）。
> 1080p 级别会到 20+ 魔粒，预算必须按预估算，不能按次数算。
>
> **大模型很慢**：14B 模型在 720×1280 上实测跑满 25 分钟仍未结束，
> 因此轮询超时默认设为 **60 分钟**（`POLL_TIMEOUT_S`），而不是 30 分钟。
>
> **上游在任务运行期间冻结魔粒**（`frozen_amount`），完成后才真正扣减。
> 余额接口返回 `{ total_balance, available_balance, frozen_amount }` 三个值，
> 判断「还能不能提交」要用 `available_balance`，不是 `total_balance`。
>
> **上游不返回真实进度**：`progress.percent` 在处理中恒为 `null`，
> 只能在任务结束时拿到 100。服务端按耗时做饱和曲线估算（上限 95%），
> 否则客户端会看到一个卡在 95% 的进度条。

---

## 2. 系统架构

```
                          ┌──────────────────────────────────────┐
   OpenAI SDK / 任意客户端 │  /v1/videos  (OpenAI 兼容层)         │
          ───────────────▶│  Bearer <自建 API Key>               │
                          └───────────────┬──────────────────────┘
                                          │
                          ┌───────────────▼──────────────────────┐
                          │  FastAPI 应用                         │
                          │  ├─ 鉴权中间件 (API Key)              │
                          │  ├─ 全局限流 (令牌桶)                  │
                          │  ├─ 任务队列 (SQLite)                 │
                          │  ├─ 调度器: 轮询 + 异常/配额切换       │
                          │  └─ 产物缓存 (本地磁盘 + TTL 清理)      │
                          └───────────────┬──────────────────────┘
                                          │
                          ┌───────────────▼──────────────────────┐
                          │  Civision 上游适配器 (httpx)           │
                          │  ├─ Muse 通路 (主)                    │
                          │  └─ Agentic 通路 (备)                 │
                          └───────────────┬──────────────────────┘
                                          │  每个账号走自己的代理出口
                          ┌───────────────▼──────────────────────┐
                          │  AccountPool: 账号 × 代理 粘性绑定      │
                          │  ├─ 会话 Cookie + 自动续期             │
                          │  ├─ 魔粒余额缓存                      │
                          │  └─ 并发上限                          │
                          └──────────────────────────────────────┘

   管理后台 (Jinja2 + HTMX)  /admin/*   ← 单管理员密码登录
```

### 2.1 目录结构

```
mdai-civision/
├── app/
│   ├── main.py                 # FastAPI 装配
│   ├── config.py               # pydantic-settings
│   ├── db.py                   # SQLAlchemy 引擎 / 会话
│   ├── models.py               # ORM: Account, Proxy, ApiKey, VideoJob, UsageLog
│   ├── schemas.py              # OpenAI 兼容 Pydantic 模型
│   ├── security.py             # API Key / 管理员鉴权
│   ├── ratelimit.py            # 令牌桶
│   ├── queue.py                # 任务队列 + worker
│   ├── scheduler.py            # 账号选择 / 轮换 / 配额切换
│   ├── storage.py              # 产物落盘 + TTL 清理
│   ├── civision/
│   │   ├── client.py           # httpx 客户端（含 CSRF / 代理 / 重试）
│   │   ├── models.py           # 模型目录拉取与缓存
│   │   ├── credits.py          # 魔粒余额 / 费率 / 流水
│   │   └── tasks.py            # 提交 / 轮询 / 取结果
│   ├── routers/
│   │   ├── videos.py           # OpenAI 兼容
│   │   ├── accounts.py         # 账号管理
│   │   ├── proxies.py          # 代理池
│   │   ├── auth.py             # GitHub OAuth 发起与回调
│   │   └── admin.py            # 后台页面
│   ├── templates/              # Jinja2
│   └── static/
├── tools/
│   ├── verify_session.py       # 探测一段 Cookie 能否打开各接口
│   ├── oauth_capture.py        # 抓 OAuth 回调实际内容 + 探测服务端换票接口
│   ├── browser_capture.py      # Playwright 采集/校验/续期（持久化 state）
│   ├── seed_state.py           # 从 Cookie 字符串生成 storage_state.json
│   └── cdp_capture.py          # 备选：从真实 Chrome profile 经 CDP 取 cookie
├── data/                       # SQLite + 产物（gitignore）
├── .browser-profile/           # 持久化浏览器 profile + storage_state.json（gitignore）
├── data/                       # SQLite + 产物（gitignore）
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
└── README.md
```

### 2.2 选型理由

- **FastAPI**：原生 async、OpenAPI 自动生成、便于暴露 `/healthz` `/metrics`
- **httpx**：支持 per-request 代理、`AsyncClient` 连接池复用
- **SQLAlchemy 2.x + SQLite**（`aiosqlite`）：单容器零依赖；模型层保持可移植性，日后可切 Postgres
- **Jinja2 + HTMX**：无构建链，后台轻量但可用；避免引入前端工具链
- **APScheduler 或自建 asyncio 循环**：账号续期、余额刷新、TTL 清理
- **Prometheus `prometheus_client`**：暴露 `/metrics`

---

## 3. 对外 API 规格

### 3.1 鉴权

`Authorization: Bearer <api_key>`，API Key 在后台创建/禁用/删除，明文仅创建时展示一次，库中存哈希。

### 3.2 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/videos` | 创建视频任务，异步，立即返回 `queued` 状态 |
| GET | `/v1/videos/{id}` | 查询任务状态 |
| GET | `/v1/videos/{id}/content` | 下载 MP4 |
| GET | `/v1/videos` | 列出任务（分页） |
| DELETE | `/v1/videos/{id}` | 删除任务及其缓存文件 |
| GET | `/v1/models` | 列出可用模型（OpenAI `Model` 列表形态） |

### 3.3 模型命名（已确认：全量导出 + sora 别名）

对外同时暴露：

1. **8 个 civision 原生模型**，命名为小写连字符形式：
   `wan2.1-t2v-1.3b` / `wan2.1-t2v-14b` / `wan2.1-i2v-14b` / `wan2.1-flf2v-14b` / `wan2.2-ti2v-5b` / `wan2.2-t2v-a14b` / `wan2.2-i2v-a14b` / `minimax-h3`
2. **sora 别名**：`sora-2` → 默认模型（默认 `wan2.2-t2v-a14b`），`sora-2-pro` → 高配模型（默认 `minimax-h3`）

别名映射表存于数据库，可在后台修改。

### 3.4 参数映射（已确认：标准值 + 额外接受原生尺寸）

**seconds**（OpenAI `4|8|12` → civision `3|5|10`）

| OpenAI seconds | civision `totalSeconds` |
|---|---|
| `4` | 3 |
| `8` | 5 |
| `12` | 10 |

> civision 页面实际提供的是 **3 / 5 / 10 秒**（前端 `totalSeconds` 选项实测）。
> **10 秒仅 MiniMax-H3 可用**（前端用 `Da.includes(model)` 控制该选项的 enabled 状态）；
> 其他模型请求 12 秒时会自动降到其支持的最长时长（5 秒）。

**size**（按**模型自身的**尺寸集合解析）

civision 每个模型支持的宽高不同，因此尺寸不是全局查表，而是**针对所选模型**在其
`videoSizeSlider` 里解析：

| 模型 | 支持的尺寸 |
|---|---|
| Wan 系列（7 个） | 720×1280 / 720×960 / 720×720 / 960×720 / 1280×720（大档）<br>480×848 / 480×640 / 480×480 / 640×480 / 848×480（小档） |
| MiniMax-H3 | 768×1280 / 768×960 / 768×768 / 960×768 / 1280×768（**仅大档**） |

解析规则：

1. 请求的宽高**模型直接支持** → 原样使用
2. 不支持 → 视为**宽高比请求**，取该模型同一比例下的尺寸，**优先大档**
3. 该比例在模型上不存在 → 回退到模型默认尺寸

| 请求 | Wan 系列 | MiniMax-H3 |
|---|---|---|
| `720x1280` | 720×1280 | 768×1280 |
| `1280x720` | 1280×720 | 1280×768 |
| `1024x1792` | 480×848 | 768×1280 |
| `1792x1024` | 848×480 | 1280×768 |
| `480x480` | 480×480 | 768×768 |
| `768x1280` | 720×1280 | 768×1280 |

> ⚠️ **上游的宽高比标签才是准的**：MiniMax-H3 把 768×1280 标为「9 : 16」，
> 但 768/1280 = 0.6，而 9/16 = 0.5625；Wan 的「9 : 16」则是 480×848（0.566）。
> 因此匹配**比例标签**而不是自己算比值，否则 768×1280 在 Wan 上会错配到 1:1。
>
> **额外接受原生尺寸**（不破坏 OpenAI 兼容）：任意命中 `NATIVE_SIZES` 的宽高都可直接传入。

### 3.5 状态映射

| civision | OpenAI `status` | `progress` |
|---|---|---|
| `PENDING` | `queued` | 0 |
| `RUNNING` | `in_progress` | 上游 `percent`（缺失时按经验值推算） |
| `SUCCEED` | `completed` | 100 |
| `FAILED` | `failed` | — |
| `CANCELED` | `failed`（error 标注 canceled） | — |

### 3.6 错误映射（已确认：严格对齐 OpenAI 语义）

| 情况 | HTTP | OpenAI error.type |
|---|---|---|
| 参数非法（prompt 空、size 不支持、模型未知） | 400 | `invalid_request_error` |
| API Key 缺失/无效 | 401 | `invalid_request_error` |
| 配额不足（魔粒耗尽） | 429 | `rate_limit_error` |
| 上游限流 / 提交过快（实测 `code: 50001`） | 429 | `rate_limit_error` |
| 账号会话失效且无法自动续期 | 401 | `invalid_request_error` |
| 上游 5xx / 网络故障 | 503 | `server_error` |
| 内部错误 | 500 | `server_error` |

**上游业务错误的判定方式**（实测）：ModelScope 很多接口 HTTP 状态码是 **200**，
真正的失败在响应体里，需按 `Code` / `Data.code` / `Data.success` 解析：

| 响应体特征 | 语义 | 映射 |
|---|---|---|
| `Data.code == 0` 且 `Data.success == true` | 成功 | 200 |
| `Data.code == 40000` | 参数错误 | 400 |
| `Data.code == 50001` | 频率过快 | 429 |
| `Data.code == 99999` | 参数/能力不支持（如 `videoInferenceArgs 仅支持 MiniMax-H3`） | 400 |
| `Code == 10010101003` | 未登录 | 401 |
| `Code == 10019901001` | 访问被拒 | 403 |

> 提交任务被限流时的实测提示：`"Request frequency too fast, please try again later"`，
> 需要间隔约 40 秒。

错误体遵循 OpenAI 格式：
```json
{ "error": { "message": "...", "type": "invalid_request_error", "code": null, "param": null } }
```

### 3.7 图生视频（已确认支持）

`input_reference` 接受：
- 完整 URL（http/https）
- base64 data URL（`data:image/png;base64,...`）

处理流程：下载/解码 → 通过 `GET /api/v1/muse/image/getImageDownloadUrl` 或 multipart 上传换取上游可访问的 URL → 放入 `imageInputFrontArgs` → `predictType` 置为 `IMG_2_VIDEO`。

**约束**：只有 `supportType` 含 `IMG_2_VIDEO` 的模型可接收；否则返回 400 并说明原因。

---

## 4. 账号与会话管理

### 4.1 账号模型

| 字段 | 说明 |
|---|---|
| `id` | 内部 ID |
| `name` | 备注名 |
| `cookie_jar` | 加密存储的 Cookie 集合（`csrf_session` / `csrf_token` / `acw_tc`） |
| `status` | `active` / `expired` / `disabled` / `quota_exhausted` |
| `proxy_id` | 绑定的代理（外键） |
| `max_concurrency` | 并发上限，默认 1 |
| `balance` / `available_balance` | 魔粒余额（缓存 + 定期刷新） |
| `last_used_at` / `last_validated_at` | 用于轮询与续期 |
| `renew_strategy` | `auto` / `manual` |

### 4.2 添加账号：两种首次登录方式（**均为一等公民**）

后台「添加账号」提供两个并列入口，任选其一：

#### 方式 A —— 手动粘贴完整 Cookie（**推荐用于首次接入 / 批量账号**）

```
[管理员] 后台「添加账号」→ 切到「粘贴 Cookie」→ 粘贴 → 「校验并保存」
```

- 粘贴内容为**完整 Cookie 字符串**，即浏览器 DevTools → Application → Cookies →
  `https://www.modelscope.ai` → 全选复制，形如：
  ```
  m_session_id=...; h_uid=...; csrf_session=...; csrf_token=...; acw_tc=...; auth_sid=...
  ```
  也接受多行粘贴、Accept-Language 风格（`#HttpOnly_` 前缀会被忽略）。
- 服务端**解析并校验**（不是照单全收）：
  1. 解析出 name/value 对，忽略 `#HttpOnly_` 前缀与空行
  2. 必须包含 **`m_session_id`** 与 **`h_uid`** → 否则判定为「非登录态」
     （因为 `csrf_session`/`csrf_token` 匿名访问也会下发）
  3. 用 `GET /api/v1/users/info` 实探一次，必须返回 200
  4. 校验通过 → 连同余额一起入库，账号状态置 `active`
- 校验失败要给出**具体原因**（缺哪个 Cookie / 未登录 / 已过期），便于管理员自查
- 粘贴的 Cookie 中，**仅 `m_session_id` + `h_uid` 为必需**；
  `csrf_*` / `acw_tc` 缺失时可由服务端后续请求自动补齐（服务端会保存响应里新下发的 `csrf_token`）

#### 方式 B —— GitHub OAuth（跨机器）

```
[管理员] 后台点「添加账号」→ 切到「一键 OAuth」→ 后端生成 oauth_url
          GET /api/v1/auth/login/generate/oauth2/url?callback_url=<服务器>/auth/github/callback&provider=github
          → 展示授权链接给管理员（管理员在任意机器的浏览器打开均可）

[管理员浏览器] GitHub 授权 → 回到 ModelScope → 302 到我们的 callback（携带 ?request_id=...）

[采集端] 后端用 Playwright + 持久化 profile 完成同一条 OAuth 流程
        → 从浏览器上下文读出完整 Cookie 集合 → 校验 → 入库
```

- `provider=github` 是唯一正确的 provider 参数名
- `callback_url` 接受任意地址（跨机器的关键）
- 持久化 profile 会记住 GitHub 登录态 → **第二次及以后可全自动**
- 判定「已登录」必须**由浏览器内调用 `/api/v1/users/info` 得到 200**，
  不能靠 `csrf_session` 是否存在（匿名访问也会拿到）

> **实测**：4s / 480×480 生成成功，成品 118781 字节合法 MP4，余额 300 → 298 魔粒。
>
> **实测**：服务端换票接口**不存在** —— 已用 baseline 严格比对 10 个候选路径
> （`/api/v1/auth/login/oauth2/{session,ticket,token,status,result,poll,check,exchange,user,info}`），
> 全部返回与伪造路径**完全相同**的 302 → `/login`。
> 故方式 B 的凭据采集阶段**必须**经由浏览器；采集完成后运行期是纯 HTTP。

**两种方式的关系**：OAuth 产出的最终结果也是一份 Cookie，因此落库结构完全一致 ——
都存 `cookie_jar`，只是采集方式不同。后续的续期、轮换、代理绑定逻辑对两者无差别。

### 4.3 登录态自动续期

会话 Cookie 的 `csrf_session` / `m_session_id` 有效期 30 天。**实测：会话是滑动续期的** —— 每次 API 响应都会重新下发 `csrf_token`（`Max-Age=2592000`），只要保持使用就会一直有效。

续期策略：

1. **主动校验**：每 N 分钟（可配，默认 15min）用 `GET /api/v1/users/info` 探测账号是否仍登录
2. **被动发现**：任一上游调用返回 `user not logged in` → 立即标记 `expired`，触发续期流程
3. **续期手段**（按可用性降级）：
   - **复用已保存 Cookie 重试**（会话滑动续期，多数情况直接奏效）
   - **Playwright 恢复**：`storage_state.json` 快照 → 注入浏览器 → 校验 → 重新导出
   - 失败 → 标记 `expired` + Webhook 告警 + 后台红点提示，由管理员重新授权
4. `acw_tc`（WAF）有效期 1800s —— 每次请求前检查并按需刷新，**不与业务会话混为一谈**

> **实测结论（重要）**：
> - Chromium 自身的 Cookie 数据库**不可靠** —— 重启后 `m_session_id` / `h_uid` 会丢失，
>   导致 headless 续期失败。
> - 因此改为**显式快照**：把完整 Cookie 集合存为 `storage_state.json`，
>   每次启动时用 `ctx.add_cookies()` 注入。
> - 已验证：注入快照后 **headless 全自动**完成校验并重新导出凭据（`status: 200`），无需人工。
> - ⚠️ **SDK Token 不可用于网页接口**。实测 `POST /v1/users/tokens/list` 可取到一个
>   有效期到 3012 年的 Token（`ms-...`，Scope=Scope），但以 Bearer / `ModelScope-Access-Token` /
>   `X-ModelScope-Token` / query 参数四种形式调用 `/api/v1/users/info` **全部 401**。
>   → 网页接口只认 Cookie 会话。

### 4.4 代理池与粘性绑定（已确认）

- 代理池：`Proxy` 表，含 `name` / `url`（支持 `http` / `https` / `socks5` / `socks5h`）/ `username` / `password` / `enabled` / `health` / `last_checked_at` / `fail_count`
- **账号粘性**：每个账号绑定一个代理，账号的**所有**上游请求都走该代理，保证该账号的出口 IP 恒定
- **分配时机**：账号首次绑定或代理失效时，从池中按「最少占用 + 健康优先」分配
- **健康检查**：后台可触发，对代理做一次探测请求；连续失败自动标记不健康并把绑定的账号迁到其他代理
- 凭据（代理用户名密码）加密存储

### 4.5 账号轮换策略（已确认：轮询 + 异常/配额切换）

选择账号的顺序：

1. 过滤：`status == active` 且 `balance > 0` 且 `running_jobs < max_concurrency`
2. 若为空 → 等待（任务进队列等待）或直接返回 429
3. 选取：按 `last_used_at` 升序（最久未用优先）实现轮询，均摊负载
4. 任务执行中若遇到：
   - 会话失效 → 换号重试（同一任务，最多重试 N 次）
   - 魔粒不足 → 标记该账号 `quota_exhausted`，换号
   - 上游 429/风控 → 该账号退避冷却（默认 60s，可配），换号
   - 上游 5xx → 换号重试（网络问题可能与出口 IP 相关）
5. 所有账号都不可用 → 任务回队列等待 + 返回可查询的排队状态

---

## 5. 后台管理（已确认：轻量 Web 后台）

单管理员 + 密码登录（密码从环境变量读取或后台首次设置，bcrypt 存储），Session Cookie。

| 页面 | 功能 |
|---|---|
| **仪表盘** | 账号数 / 可用账号 / 总魔粒余额 / 近 24h 生成数与消耗 / 队列深度 / 上游成功率 |
| **账号管理** | 列表（状态、余额、代理、并发、最后使用）、新增（**粘贴 Cookie** / **一键 OAuth** 两个并列入口）、编辑、启用禁用、删除、手动校验会话、手动刷新余额 |
| **代理池** | 增删改、启用禁用、健康检查、一键分配、查看绑定关系 |
| **API Key** | 创建（明文只显示一次）、禁用、删除、使用量统计 |
| **模型与映射** | 实时拉取的模型目录、sora 别名映射、尺寸/时长映射表编辑、魔粒费率表 |
| **任务列表** | 全部视频任务：状态、账号、模型、尺寸、耗时、魔粒消耗、产物链接；支持筛选与导出 CSV |
| **用量统计** | 按账号 / 模型 / 日期聚合的魔粒消耗；费率表 vs 预估 vs 实际的对比 |
| **设置** | 全局限流、续期间隔、TTL 保留期、告警 webhook、冷却时长 |

---

## 6. 可靠性、可观测性

### 6.1 已确认的项

- **每账号并发上限**（默认 1，后台可配）
- **生成任务队列**：SQLite 持久化队列，进程重启不丢任务；worker 池执行；对外可见排队位置
- **登录态自动续期 + 告警**
- **全局限流**：令牌桶，每分钟最大请求数（后台可配）
- **生成记录与用量统计**：每次生成落一条记录（账号、模型、尺寸、时长、耗时、魔粒消耗、状态）
- **结构化日志**：JSON 行，带 `trace_id` / `account_id` / `job_id`
- **Webhook 告警**：账号失效、余额不足、代理故障、上游 5xx 率超阈值；支持钉钉 / 飞书 / Slack 通用 JSON
- **健康检查 + Prometheus 指标**：`/healthz`、`/metrics`

### 6.2 产物存储（已确认：本地缓存 + TTL 自动清理 + 上游 URL 双轨）

- 视频文件落本地 `data/videos/<job_id>.mp4`
- 默认保留 **7 天**（可配），后台定时清理
- 同时保留上游 CDN URL 字段，可直下（受上游有效期限制）
- `GET /v1/videos/{id}/content` 优先读本地；本地已清理时，若上游 URL 仍有效则回源转发

---

## 7. 部署

- **单容器**：`Dockerfile` 打包 FastAPI + worker + 定时任务；`docker-compose.yml` 挂载 `data/` 与 `playwright` profile 卷
- 环境变量：`ADMIN_USERNAME` / `ADMIN_PASSWORD` / `SECRET_KEY` / `DATABASE_URL` / `STORAGE_DIR` / `PUBLIC_BASE_URL`（OAuth 回调地址要用）/ `RATE_LIMIT_RPM` / `VIDEO_TTL_DAYS` / `ALERT_WEBHOOK_URL` 等

---

## 8. 待验证清单

### 8.1 已解决 ✅

| # | 问题 | 结论 |
|---|---|---|
| 1 | Cookie 会话能否打开视频接口？ | ✅ **能**。完整 Cookie 集合在纯 httpx 中复现成功 |
| 2 | `X-CSRF-TOKEN` 契约是否成立？ | ✅ 成立。`csrf_token` URL 解码后作头值 |
| 3 | 提交/轮询/取结果全链路？ | ✅ 跑通：`submit → taskId → status → SUCCEED → mp4` |
| 4 | 魔粒预估与实际扣费是否一致？ | ✅ 一致（预估 2，实际扣 2） |
| 5 | 是否存在 `request_id` 的服务端换票接口？ | ✅ **不存在**（10 个候选路径全部等同 baseline） |
| 6 | 真实登录态由哪些 Cookie 承载？ | ✅ `m_session_id` + `h_uid`（csrf 系列匿名也会下发） |
| 7 | SDK Token 能否替代 Cookie？ | ✅ **不能**（四种鉴权形式全部 401） |
| 8 | 会话是否滑动续期？ | ✅ 是，每次响应重发 `csrf_token`（30 天） |
| 9 | headless 自动续期能否无人值守？ | ✅ **能**（显式 `storage_state.json` 快照 + `add_cookies`） |
| 10 | Chromium 自带 Cookie 库能否跨重启持久化？ | ❌ **不能**（`m_session_id`/`h_uid` 会丢），必须用显式快照 |

### 8.2 仍待验证

| # | 待验证项 | 影响 | 验证方法 |
|---|---|---|---|
| 1 | Playwright 无人工续期能否稳定完成 | 决定「自动续期」全自动还是需人工介入 | 已有持久化 profile，跑 `tools/browser_capture.py --headless` |
| 2 | 1080p / 更大尺寸的魔粒消耗 | 费率预估准确度 | 后台实际用量统计（已知 720×1280 = 23 魔粒） |
| 3 | `totalSeconds` 8 / 12 的实际耗时与消耗 | seconds 映射 | 提交试跑 |
| 4 | 图生视频（IMG_2_VIDEO）的完整链路与图片上传方式 | `input_reference` 支持 | 用真实图片试跑 |
| 5 | MiniMax-H3 专属的 `videoInferenceArgs` 语义 | FLF2V / 多参考图能力 | 单独试跑 |
| 6 | 提交频率限制的精确阈值（实测 ~40s） | 限流退避策略 | 逐步加压测试 |
| 7 | 大模型的最长耗时 | 轮询超时 | 已知 14B@720p > 25 min |

### 8.3 实现期间发现的坑

| 坑 | 现象 | 处理 |
|---|---|---|
| `csrf_session` 匿名也会下发 | 用它判登录态会误判 | 必须看 `m_session_id` + `h_uid` |
| 提交体 camelCase，预估体 snake_case | 预估接口报「需要传入 video_predict_task_request」 | 两套字段名分开处理 |
| `videoInferenceArgs` 仅 MiniMax-H3 | 其他模型报 99999 | 仅 H3 传该字段 |
| 上游错误在 HTTP 200 的 body 里 | 只看状态码会当成功 | 解析 `Code` / `Data.code` / `success` |
| SQLite 无时区 | naive/aware 相减抛 TypeError | 自定义 `UtcDateTime` 类型统一为 UTC-aware |
| `create_all` 不改已有表 | 加字段后启动报 no such column | 加了 `app/migrate.py` 轻量迁移 |
| Chromium 自带 Cookie 库不可靠 | 重启后 `m_session_id` 丢失 | 改用显式 `storage_state.json` 快照 |
| 进程重启后 `running_jobs` 泄漏 | 所有账号看起来都在忙，队列卡死 | 启动时清零计数器 |
| 任务跨重启会被重复提交 | 白白消耗魔粒 | 记录 `upstream_task_id`，重启后接着轮询 |

---

## 9. 已确认的需求决策汇总

| 决策项 | 结论 |
|---|---|
| 反代方式 | 纯内部 API 反代（不走 UI 自动化生成） |
| 登录方式 | **仅 GitHub OAuth**（阿里云/邮箱密码因验证码与账号体系问题排除） |
| 技术栈 | Python + FastAPI |
| 对外接口范围 | 完整 OpenAI 生命周期（create / retrieve / content / list / delete / models） |
| 凭据获取 | 先做 1 分钟人工验证，工具已就绪 |
| 轮换策略 | 轮询 + 异常/配额不足时切换 |
| 代理形态 | 代理池 + 账号粘性分配 |
| 后台形态 | 轻量 Web 后台（Jinja2 + HTMX） |
| 参数映射 | 标准值 + 额外接受原生尺寸 |
| 图生视频 | 支持 |
| 魔粒展示 | 费率表 + 预估 + 实际用量统计 |
| 调度 | 每账号并发上限 + 任务队列 + 自动续期告警 + 全局限流 |
| 存储 | SQLite + Docker 单容器 |
| 鉴权/产物 | 自建 API Key + 本地缓存（TTL 清理）双轨上游 URL |
| 后台鉴权 | 单管理员 + 密码 |
| 错误映射 | 严格对齐 OpenAI 语义 |
| 可观测性 | 用量统计 + 结构化日志 + Webhook 告警 + /healthz + /metrics |
