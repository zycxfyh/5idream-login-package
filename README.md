# 5idream（到梦空间）扫码登录 · FastAPI 服务

把原来的命令行扫码登录工具 `5idream_login.py` 改造成 **FastAPI Web 服务**。

- **异步化 / 高并发**：全程 `httpx` 异步 + `asyncio.sleep`，不阻塞事件循环；
- **token 个人缓存**：登录成功后按 `user_id` 缓存 5idream token，TTL 空闲过期、重复登录自动刷新、退出即清除；
- **防击穿**：同一用户的并发请求共用一把锁 + 短期数据缓存，不会重复轰炸 5idream 接口；
- 附极简网页 UI（`/`），可直接扫码测试。

> 仅用于本人有权使用的账号与合法合规的个人数据整理，勿分享二维码、Token 与导出数据。

---

## 1. 安装与启动

```powershell
cd C:\Users\Administrator\Desktop\Fastapi\5idream-fastapi
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

启动后访问：http://127.0.0.1:8000/

> 不想用 venv 也可以直接 `pip install -r requirements.txt`。
> 生产环境建议 `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1`（内存缓存单进程共享，多 worker 需换 Redis 实现）。

## 2. 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/login/qr` | 创建扫码登录会话，返回 `session_id` + 二维码 |
| GET | `/api/v1/login/status/{session_id}` | 轮询扫码结果（客户端每 2~3 秒调一次） |
| GET | `/api/v1/activity-categories` | 活动分类目录（分库 key，公开） |
| GET | `/api/v1/tribe-categories` | 部落分类目录（分库 key，公开） |
| GET | `/api/v1/activities?category=&page=&page_size=` | **某个活动分类的某一页**（分库分页） |
| GET | `/api/v1/tribes?category=&page=&page_size=` | **某个部落分类的某一页**（分库分页） |
| GET | `/api/v1/activities/{activity_id}` | 活动详情 |
| GET | `/api/v1/data` | 无 `category`：个人信息 + 分类目录；带 `category`：该分类整理后分页 |
| GET | `/api/v1/report` | Markdown 汇总报告（需一次拉取全部分类，较慢，低频用） |
| GET | `/api/v1/me` | 当前登录用户信息 |
| POST | `/api/v1/logout` | 退出登录（移除 token 缓存） |
| GET | `/healthz` | 健康检查 |

### 分库分页用法

活动/部落/整理数据均**按分类独立查询、按页请求**，不再一次拉取全部：

```text
① GET /api/v1/activity-categories
   → { "categories": [ { "key": "join", "label": "我报名的" }, ... ] }
② GET /api/v1/activities?category=join&page=1&page_size=10
   → { "kind": "activity", "category": "join", "label": "我报名的",
       "page": 1, "page_size": 10, "total": 37, "total_pages": 4,
       "records": [ ...该页10条原始字段... ] }
③ 上一页/下一页：改 page 参数即可（page 从 1 开始，page_size 1~100）
```

- 部落分类 key：`manage`（我管理的）、`join`（我加入的）
- 整理数据：`/api/v1/data?category=join&page=1&page_size=10` 返回该分类整理后的精简字段（与 `5idream-data.json` 同口径），无 `category` 时即时返回个人信息 + 分类目录，不请求 5idream
- 每页结果按「用户+分类+页码+每页条数」缓存 60 秒，翻页各自独立、并发不重复请求；加 `&refresh=true` 强制刷新本页

交互式 API 文档：http://127.0.0.1:8000/docs

## 3. 登录流程（三步）

```text
① POST /api/v1/login/qr
   → { session_id, qr_image_base64, qr_payload, expires_in }

② 展示二维码，用「到梦空间」APP 扫码并确认
   客户端每 2~3 秒 GET /api/v1/login/status/{session_id}
   → 未确认：{ status: "pending" }
   → 确认后：{ status: "success", token, user }   ← 服务端已把 token 写入个人缓存

③ 数据接口鉴权：请求头携带  Authorization: Bearer <token>
```

示例（PowerShell）：

```powershell
# 1. 生成二维码
$qr = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/login/qr
# 打开 $qr.qr_image_base64 对应的二维码，用 APP 扫码确认

# 2. 轮询（确认前会返回 pending）
$s = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/login/status/$($qr.session_id)"
$s.status          # success 后
$token = $s.token

# 3. 分页取数据（按分类、按页）
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/activities?category=join&page=1&page_size=10" `
  -Headers @{ Authorization = "Bearer $token" }
# 翻页：把 page 改成 2、3……
```

## 4. 配置（.env，可全部省略）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BASE_URL` | `https://www.5idream.net` | 站点地址 |
| `REQUEST_DELAY_RANGE` | `0.5,1.5` | 每次请求前随机等待秒数（频率控制） |
| `LOGIN_TIMEOUT_SECONDS` | `90` | 等待扫码确认的最大秒数 |
| `MAX_ROWS` | `100` | 每类数据请求最大条数 |
| `TOKEN_IDLE_TTL` | `3600` | token 个人缓存空闲过期秒数（0=不过期） |
| `DATA_CACHE_TTL` | `60` | 分页查询结果缓存秒数（0=不缓存） |
| `SESSION_TTL` | `1800` | 登录会话保留秒数 |
| `CORS_ORIGINS` | `*` | 允许跨域来源 |

## 5. 目录结构

```text
5idream-fastapi/
├── run.py                # 本地启动入口
├── requirements.txt
├── .env.example          # 配置样例（复制为 .env）
└── app/
    ├── main.py           # FastAPI 应用、路由、鉴权、极简 UI
    ├── client.py         # 5idream 异步客户端（httpx）
    ├── cache.py          # token 个人缓存 / 登录会话 / 数据缓存
    ├── formatters.py     # 字段翻译、报告渲染（原样移植）
    └── config.py         # 配置
```

## 6. 与原 CLI 的差异

| 原 CLI | 本服务 |
|---|---|
| 同步 `requests.Session` | 异步 `httpx`，请求显式携带 token cookie，用户间隔离 |
| `time.sleep(1~2s)` 阻塞 | `asyncio.sleep` 随机等待，不阻塞事件循环 |
| token 只存单个会话内存 | 按 `user_id` 个人缓存，TTL 过期 + 重复登录刷新 + 可登出 |
| 交互式 `input()` 菜单 | REST API + 极简网页 UI |
| 一次性拉全部分类并导出文件 | 分库分页查询：按分类 key + page/page_size 单页请求，每页独立缓存 |
