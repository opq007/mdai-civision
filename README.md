# mdai-civision

把 **ModelScope 魔搭社区海外版（Civision）** 的网页端视频生成能力，包装成一个可独立调用、
符合 **OpenAI Videos API** 标准的接口，并附带多账号 / 代理 / 魔粒额度管理后台。

> 官方公开 API 只覆盖 LLM 与文生图，**没有视频模型**。
> 本项目封装的是网页端同源的内部接口（Cookie 会话鉴权）。

完整规格见 [SPEC.md](SPEC.md)。

---

## 已验证的能力

以下均已用真实账号端到端跑通：

| 能力 | 验证结果 |
|---|---|
| 提交视频任务 | ✅ `POST /api/v1/muse/predict/task/video/submit` → `taskId` |
| 轮询任务状态 | ✅ `PROCESSING → UNDER_REVIEW → SUCCEED` |
| 产出视频文件 | ✅ 合法 MP4（`ftypisom`），已下载并本地缓存 |
| 魔粒余额查询 | ✅ 预估值与实际扣减**完全一致** |
| 魔粒费率表 | ✅ `aigc_video` / `standard` / `1 魔粒每次`（起步价） |
| 模型目录 | ✅ 8 个视频模型（Wan2.1 / Wan2.2 / MiniMax-H3），实时拉取 |
| 跨机器 OAuth 回调 | ✅ `callback_url` 接受任意地址 |
| 无人工自动续期 | ✅ headless 恢复快照后校验通过（HTTP 200） |
| OpenAI 兼容层 | ✅ 完整的 create / retrieve / content / list / delete / models |

---

## 快速开始

### 1. 安装

```bash
pip install -e .
playwright install chromium      # 仅 OAuth 采集需要
cp .env.example .env             # 至少改 SECRET_KEY 和 ADMIN_PASSWORD
```

### 2. 采集一个 ModelScope 账号的会话

两种方式，**任选其一**：

**A. 手动粘贴 Cookie（最快）**

在浏览器里登录 https://www.modelscope.ai ，然后 DevTools → Application →
Cookies → `https://www.modelscope.ai` → 全选复制，得到形如
`m_session_id=...; h_uid=...; csrf_session=...` 的字符串。

> ⚠️ 真正承载登录态的是 **`m_session_id`** 和 **`h_uid`**。
> `csrf_session` / `csrf_token` 匿名访问也会下发，**不能**用来判断是否已登录。

打开后台 → Accounts → **Add an account → paste cookies** → 粘贴 → *Validate and add*。
服务端会当场拿这份凭据去 ModelScope 实探一次，通过了才入库。

**B. GitHub OAuth（可无人值守）**

```bash
python tools/browser_capture.py       # 首次需人工登录一次 GitHub
python tools/browser_capture.py --headless   # 之后可全自动
```

### 3. 启动

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# 或
docker compose up -d
```

> 镜像默认包含 Chromium（约 400 MB），用于 GitHub OAuth 采集与无人工续期。
> 如果你只用「粘贴 Cookie」添加账号，可以跳过它：
> `docker compose build --build-arg INSTALL_BROWSER=false`

打开 http://127.0.0.1:8000/admin ，用 `.env` 里的 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录。

> 未登录时访问 `/admin` 会**跳转到 `/admin/login`** 登录页（浏览器看到的是表单，
> 不是 JSON 报错）。直接访问 http://127.0.0.1:8000/admin/login 也可以。

### 4. 创建 API Key 并调用

在 **API keys** 页面创建一把 key（明文只显示一次），然后：

```bash
curl -X POST http://127.0.0.1:8000/v1/videos \
  -H "Authorization: Bearer sk-civ-..." \
  -H "content-type: application/json" \
  -d '{"model":"sora-2","prompt":"a cat playing piano","seconds":"4","size":"720x1280"}'
```

返回 OpenAI 标准的 `VideoResource`：

```json
{
  "id": "video_8fed66c36c974878bac3d8b7",
  "object": "video",
  "model": "sora-2",
  "status": "queued",
  "progress": 0,
  "created_at": 1792644875,
  "completed_at": null,
  "expires_at": null,
  "prompt": "a cat playing piano",
  "size": "720x1280",
  "seconds": "4",
  "remixed_from_video_id": null,
  "error": null
}
```

轮询 `GET /v1/videos/{id}` 直到 `status` 变成 `completed`，然后
`GET /v1/videos/{id}/content` 取回 MP4。

> 查询状态和下载 MP4 **不需要 API Key**：只要知道 `video_id` 就能直接访问，
> 方便把链接丢给浏览器、播放器或第三方页面。加 `?download=true` 强制下载，
> 不加则以内联方式返回（可直接作为 `<video src>` 使用）。

---

## 接口

### OpenAI 兼容

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/v1/videos` | 🔑 | 创建任务，立即返回 `queued` |
| GET | `/v1/videos/{id}` | 🔓 | 查询状态 |
| GET | `/v1/videos/{id}/content` | 🔓 | 下载 MP4 |
| GET | `/v1/videos` | 🔑 | 列出任务 |
| DELETE | `/v1/videos/{id}` | 🔑 | 删除任务与缓存 |
| GET | `/v1/models` | 🔑 | 列出可用模型 |

鉴权：`Authorization: Bearer <api key>`。

🔓 = **不需要 API Key**。`video_id` 是不可猜测的随机串，本身即凭证，
所以查询和下载可以直接暴露给外部调用方。写操作仍然全部需要 API Key。

### 模型

开放 4 个，外加两个 OpenAI 别名：

| 名称 | 指向 | 能力 | 时长 | 加速 | 步数 |
|---|---|---|---|---|---|
| `wan2.1-t2v-1.3b` | Wan2.1-T2V-1.3B | 文生视频 | 3 / 5 秒 | — | 20 |
| `wan2.1-i2v-14b` | Wan2.1-I2V-14B-720P | **图生视频** | 3 / 5 秒 | — | 20 |
| `minimax-h3-turbo` | MiniMax-H3 | 文生 / 图生 / 首尾帧 | 3 / 5 / 10 秒 | **开** | 4 |
| `minimax-h3` | MiniMax-H3 | 文生 / 图生 / 首尾帧 | 3 / 5 / 10 秒 | 关 | 20 |
| `sora-2` | → `minimax-h3-turbo` | 别名 | 3 / 5 / 10 秒 | 开 | 4 |
| `sora-2-pro` | → `minimax-h3` | 别名 | 3 / 5 / 10 秒 | 关 | 20 |

> **两个 `minimax-h3` 是同一个上游模型**，区别只在网页上的「LoRA 推理加速」开关：
> 打开时采样步数被钉在 4、价格减半；关闭时按模型自己的 20 步跑。
> 两种设置出片效果差别明显，所以拆成两个模型名而不是一个请求参数。
>
> `sora-2` / `sora-2-pro` 指向 MiniMax-H3 的两个变体，而不是 Wan 系列 ——
> OpenAI 的 Sora 本身就是「快版 / 增强版」两档，语义上更接近。

上游还有 5 个视频模型不对外开放。要放开的话，编辑
`app/civision/mapping.py` 的 `ALLOWED_MODELS`。

### 魔粒消耗

用上游的免费预估端点实测，与网页显示一致。后台「Models」页有完整表格。

**Wan2.1-T2V-1.3B（文生视频）**

| 尺寸 | 3s | 5s | 10s |
|---|---|---|---|
| 720×1280 / 720×960 / 720×720 / 960×720 / 1280×720 | 3 | 6 | 11 |
| 480×848 / 848×480 | 3 | 6 | 11 |
| 480×640 / 480×480 / 640×480 | 2 | 3 | 5 |

**Wan2.1-I2V-14B（图生视频）**

| 尺寸 | 3s | 5s | 10s |
|---|---|---|---|
| 720×1280 / 1280×720 / 480×848 / 848×480 | 17 | 28 | 56 |
| 720×960 / 720×720 / 960×720 | 17 | 28 | — |
| 480×640 / 480×480 / 640×480 | 8 | 13 | 25 |

**MiniMax-H3 turbo（`minimax-h3-turbo`，开加速，4 步）**

| 尺寸 | 3s | 5s | 10s |
|---|---|---|---|
| 全部 5 种 | 34 | 56 | 113 |

**MiniMax-H3（`minimax-h3`，关加速，20 步）**

| 尺寸 | 3s | 5s | 10s |
|---|---|---|---|
| 全部 5 种 | 68 | 113 | 225 |

消耗只取决于**像素总量**和**时长**，与宽高比无关。
网页对 Wan 系列隐藏了 10 秒选项（标注「敬请期待」），但接口实际接受 —— 服务层
仍只对 MiniMax-H3 开放 10 秒，与网页行为一致。

关掉加速后步数从 4 涨到 20，价格随之翻倍（3s：34 → 68）。

### 参数映射

**时长** —— OpenAI 的 `4` / `8` / `12` 映射到 civision 的 `3` / `5` / `10` 秒。
`10` 秒只有 MiniMax-H3 支持，其他模型会自动降到 5 秒。

**尺寸** —— 按**所选模型自己支持的尺寸集合**解析，不是全局查表。
Wan 系列有两档（720 档 / 480 档），MiniMax-H3 只有 768 档：

| 请求 | Wan 系列 | MiniMax-H3（两个变体相同） |
|---|---|---|
| `720x1280` | 720 × 1280 | 768 × 1280 |
| `1280x720` | 1280 × 720 | 1280 × 768 |
| `1024x1792` | 480 × 848 | 768 × 1280 |
| `1792x1024` | 848 × 480 | 1280 × 768 |
| `480x480` | 480 × 480 | 768 × 768 |
| `768x1280` | 720 × 1280 | 768 × 1280 |

规则：模型直接支持就原样用；不支持就按**宽高比**取该模型同一比例的尺寸（优先大档）；
该比例不存在则回退到模型默认尺寸。

额外也接受 civision 的原生宽高（如 `480x480`、`960x720`、`720x960`）。

**采样步数** —— 服务端默认值：Wan 系列 20 步（与不加速的 MiniMax-H3 一致），
`minimax-h3-turbo` 4 步（加速），`minimax-h3` 20 步（不加速）。
civision 页面本身提供 1–50 的滑块、默认 50；Wan 降到 20 是为了速度和魔粒
（免费估算实测 480×480/3s 由 2 降到 1，720×1280/5s 由 6 降到 2）。
请求里可以用 `num_inference_steps` 覆盖，建议保持在 1–50；但加速打开时
（`minimax-h3-turbo`）步数会被钉死为 4，与页面行为一致。

### 首尾帧（MiniMax-H3）

上游没有 `FLF_2_VIDEO` 这个 predictType —— 网页的「首尾帧」开关，实质是
`IMG_2_VIDEO` 加上第二张图。本服务对应地多接受一个 `input_reference_end`：

```bash
curl -X POST http://127.0.0.1:8000/v1/videos \
  -H "Authorization: Bearer sk-civ-..." \
  -H "content-type: application/json" \
  -d '{
    "model": "minimax-h3",
    "prompt": "the bird flies away",
    "seconds": "8",
    "size": "720x1280",
    "input_reference":      {"image_url": "https://example.com/start.png"},
    "input_reference_end":  {"image_url": "https://example.com/end.png"}
  }'
```

两张图分别进上游的 `imageInputFrontArgs` 与 `extraImageInputFrontArgs`，
和网页发出的请求体一致。`input_reference_end` 是**扩展字段**，不在 OpenAI
标准里；只有 `minimax-h3` 接受，且必须同时给 `input_reference`，
否则返回 400。魔粒消耗与同尺寸同时长的单图任务相同。

### 错误语义

严格对齐 OpenAI：

| 情况 | HTTP | type |
|---|---|---|
| 参数非法 / 模型不存在 / 校验失败 | 400 | `invalid_request_error` |
| API Key 缺失或无效（仅写操作） | 401 | `invalid_request_error` |
| 视频未就绪 | 409 | `invalid_request_error` |
| 魔粒不足 / 上游限流 | 429 | `rate_limit_error` |
| 上游 5xx | 503 | `server_error` |

错误体固定为 `{"error": {"message", "type", "code", "param"}}`。

---

## 后台

| 页面 | 功能 |
|---|---|
| Dashboard | 余额、账号状态、24h 生成量与魔粒消耗、队列深度 |
| Accounts | 粘贴 Cookie / OAuth 新增、校验、启停、代理绑定 |
| Proxies | 代理池增删、健康标记、按账号粘性分配 |
| API keys | 创建 / 删除，明文只显示一次 |
| Models | 实时模型目录、别名、尺寸映射 |
| Usage | 任务列表、魔粒消耗、错误记录 |

---

## 运行期须知

- **提交有频率限制**。上游拒绝过快的提交（`Request frequency too fast`），实测同一账号
  约需间隔 45 秒。因此**多账号不是可选优化，而是吞吐量的必要条件**。
- **魔粒消耗由分辨率、时长和采样步数共同决定**，不是费率表里写的固定「1 魔粒每次」（那是起步价）。
  完整表格见上面的「魔粒消耗」小节。

  提交前会先调 `estimate-quota` 预估并检查余额，不够就不提交。
  预估用的请求体与提交请求体**逐字段一致**，所以预估就是实付。
- **上游在任务运行时冻结魔粒**（`frozen_amount`），完成后才真正扣减。
- **大模型很慢**。Wan2.2-T2V-A14B 在 720×1280 上可能跑十几分钟；上游不返回真实百分比，
  服务端按时间估算进度。
- **会话是滑动的**：每次请求都会重新下发 `csrf_token`（30 天），保持使用即可续期。

> ⚠️ **不要用真实生成来测试**。魔粒是真实消耗品，一次 10 秒的 720p 就能烧掉上百。
> 验证请用纯函数断言（`resolve_size` / `resolve_seconds`）和免费的
> `estimate-quota` 端点。详见 [AGENTS.md](AGENTS.md)。

---

## 工具

| 工具 | 用途 |
|---|---|
| `tools/browser_capture.py` | **主力**。Playwright 采集 / 校验 / 无人工续期 |
| `tools/verify_session.py` | 探测一段 Cookie 能打开哪些接口 |
| `tools/seed_state.py` | 从 Cookie 字符串生成 `storage_state.json` |
| `tools/oauth_capture.py` | 诊断：抓 OAuth 回调 + 探测服务端换票接口 |
| `tools/cdp_capture.py` | 备选：经 CDP 从真实 Chrome profile 取 cookie |

---

## 运维

| 端点 | 说明 |
|---|---|
| `GET /healthz` | 存活检查，含账号状态统计 |
| `GET /metrics` | Prometheus 指标 |

后台定时任务：会话校验与余额刷新（默认 15 分钟）、产物 TTL 清理（默认 7 天）。

配置项见 [.env.example](.env.example)。

---

## 测试

```bash
python -m pytest tests/ -q
```

---

## License

仅供内部研究与开发使用。请遵守 ModelScope 的服务条款与账号使用政策。
