# Ink2Reel — 基于 ArcReel 内核的无人值守文本链

本目录是 **ArcReel** 的本地 fork（基线 commit `b0a2ce5b18232db11ca3c585b9df824caab22f54`），外加旁路包 `ink2reel_driver/`。

- **零改动原则**：`lib/`、`server/` 等 ArcReel 原有代码默认不改；差异化逻辑全部写在 `ink2reel_driver/`。
- **本棒范围**：文本链无人值守（小说 → 分集 → step1 → 审核门 → 剧本 JSON → 交付物文本三件套）。**不触发**图片/视频生成。

## 起服务

```powershell
cd ink2reel
uv sync --group dev
# 单机可关鉴权
$env:AUTH_ENABLED="false"
.\.venv\Scripts\uvicorn.exe server.app:app --host 127.0.0.1 --port 1241
```

- API 文档：`http://127.0.0.1:1241/docs`
- 默认端口：`1241`

## 接入 DeepSeek（custom provider）

1. 服务已启动后，注册 OpenAI 兼容文本供应商：

```powershell
# 将 $env:DEEPSEEK_API_KEY 设为你的 Key（勿写入仓库）
$body = @{
  display_name = "DeepSeek"
  discovery_format = "openai"
  base_url = "https://api.deepseek.com/v1"
  api_key = $env:DEEPSEEK_API_KEY
  image_max_workers = $null
  video_max_workers = $null
  audio_max_workers = $null
  models = @(
    @{
      model_id = "deepseek-chat"
      display_name = "DeepSeek Chat"
      endpoint = "openai-chat"
      is_default = $true
      is_enabled = $true
    }
  )
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Uri "http://127.0.0.1:1241/api/v1/custom-providers" -Method POST -Body $body -ContentType "application/json"
```

2. 连通性测试：`POST /api/v1/custom-providers/{id}/test` 或 `POST /api/v1/custom-providers/test`。
3. 将系统默认文本后端指到 `custom-{id}/deepseek-chat`：

```powershell
$cfg = @{ default_text_backend = "custom-1/deepseek-chat"; text_backend_complex = "custom-1/deepseek-chat"; text_backend_simple = "custom-1/deepseek-chat" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:1241/api/v1/system/config" -Method PATCH -Body $cfg -ContentType "application/json"
```

> Key 只进 ArcReel 凭据存储（DB）或环境变量；禁止写入代码、报告、git。

## 一键文本链

在 `ink2reel/` 目录、服务不必必须运行（driver 直调 `lib/` + 本地 SQLite 配置）：

```powershell
cd ink2reel
.\.venv\Scripts\python.exe -m ink2reel_driver run `
  "..\ink2reel-evaluation\runs\inkos\export\午夜便利店_export.txt" `
  --project midnight2 --until script --gate auto
```

产出：

- `projects/<name>/scripts/episode_1.json` — 合法说书剧本（`NarrationEpisodeScript`）
- `projects/<name>/driver_journal.jsonl` — 阶段起止 / gate / **step1_method**
- `projects/<name>/deliverables/EP-001/` — 交付文件夹（见下）

### 源文清洗

上传时：

- 原文 → `source/raw_<原文件名>`
- 清洗版 → `source/<原文件名>`（planner / step1 消费）

清洗规则（`ink2reel_driver/textclean.py`）：去掉 markdown 标题行、去掉行首 `>`、连续 ≥3 空行压为 1。

### step1 边界切分

LLM **不**输出全文 `novel_text`，只输出每段 `end_quote`（原文一字不差的段尾短句）+ 元数据；driver 用 `str.find` 定位 offset，**代码从原文切片**生成 `novel_text`，逐字覆盖由构造保证。

| journal 字段 | 含义 |
| --- | --- |
| `step1_method: llm_boundary` | 边界切分成功（验收硬标准） |
| `step1_method: mechanical_fallback` | 3 次反馈重试后机械切分保底 |
| `retries` | 反馈重试次数（0=一次成功） |

观测：

```powershell
python -c "import json;print([json.loads(l)['detail'] for l in open('projects/midnight2/driver_journal.jsonl',encoding='utf-8') if 'step1_method' in l])"
```

Gate 模式：

| `--gate` | 行为 |
| --- | --- |
| `auto` | 自动确认 step1（`approved_by: auto`） |
| `manual` | 打印待办并以退出码 **2** 停下 |
| `threshold` | 质检门：机械规则 + LLM 六维；≥`--threshold`（默认 50）auto-pass，否则 **escalate** 进异常队列（exit 0，不停机） |

```powershell
# 质检门（复用既有剧本/资产，不重烧图时加 --no-worker 且已有产物会幂等跳过）
.\.venv\Scripts\python.exe -m ink2reel_driver run `
  "..\ink2reel-evaluation\runs\inkos\export\午夜便利店_export.txt" `
  --project midnight2 --until storyboard --gate threshold --threshold 50 --limit-segments 4 --no-worker
```

## 质检层（`ink2reel_driver/qc/`）

| 模块 | 作用 | 来源 / 校准 |
| --- | --- | --- |
| `rules_prompt` | 长度 1500 block / 1200 warn；敏感词 auto-fix 或 block | OnlyShot `scripts/生成分镜图.py:41-56` + `references/jimeng-failure-modes.md:65-114`；**PROMPT_LEN=1500 为 N=8 实测、换供应商需重校准** |
| `rules_ref` | 缺角色/场景图 block；ref≥3 warn、≥5 block | OnlyShot 一致性经验；**3+/5 阈值为 N=8 实测** |
| `rules_script` | novel 40–400 字、空 prompt、总时长 60s±15%（满集硬拦）、同 duration 连击、相邻 prompt 相似 | 计划书机械规则 |
| `judge` | 六维 0–10 求和（0–60）：钩子/可预测/同质化/爽点/辨识度/老套 | OnlyShot `references/critic-checklist.md`；解析失败 → `judge_unavailable` → escalate（**禁止默认 0 分或默认放行**） |

### 异常队列 CLI

escalate 写入 `projects/<name>/exceptions.jsonl`。

```powershell
.\.venv\Scripts\python.exe -m ink2reel_driver exceptions list --project midnight2
.\.venv\Scripts\python.exe -m ink2reel_driver exceptions resolve <id> --project midnight2 --action approve|regenerate|skip
```

- `approve`：回写 `project.json` 的 `qc_approvals`
- `regenerate` / `skip`：落 journal `exception_resolve` 待消费

## 投喂队列（`queue/`）

```text
ink2reel/queue/
├── inbox/              # 投放 <name>.txt|.md + 可选 <name>.json sidecar
├── processing/         # claim 中
├── done/<project>/     # 成功无 escalate → result.json
├── attention/<project>/# 成功但 escalate → result.json + exceptions 摘要
├── failed/<project>/   # 失败 → error.txt
├── queue_journal.jsonl
└── .lock               # 单实例锁（PID+ts；>2h 且 PID 死 → 自清）
```

### sidecar（均可缺省）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `until` | `storyboard` | `storyboard` / `deliverables` / … |
| `gate` | `threshold` | `threshold` / `auto` |
| `threshold` | `50` | 六维总分下限 |
| `budget_usd` | `5` | 媒体预算 |

项目名 = 文件名 slug + 内容 sha256 前 6 位（例：`午夜便利店-a1b2c3`）。

### 消费命令

```powershell
# 单次（适合计划任务）
.\.venv\Scripts\python.exe -m ink2reel_driver watch --once

# 常驻（每 5 分钟扫一次；Ctrl+C 释放锁）
.\.venv\Scripts\python.exe -m ink2reel_driver watch --interval 300
# 或：uv run python -m ink2reel_driver watch --interval 300
```

**不自动注册系统计划任务**。若需 Windows 计划任务，由用户自行执行（示例）：

```bat
schtasks /Create /SC MINUTE /MO 5 /TN Ink2ReelQueue /TR "cmd /c cd /d D:\demo\漫剧流水线\ink2reel && uv run python -m ink2reel_driver watch --once"
```

### codex 对接

- 扫 `queue/done/*/result.json` 与 `queue/attention/*/result.json`
- 或扫 `projects/*/deliverables/*/SUMMARY.md`（仅当 timeline/视频契约全 ready 再消费成片）

崩溃自愈：启动时 `processing/` 遗留 → 回 `inbox` 加 `.retry1`；`.retry2` 仍败 → `failed/`。

## 媒体链（图 / 视频 / TTS）

顺序：`script → (script_qc if threshold) → assets → storyboard(+段级 qc) → video → tts → export`

```powershell
# 限 4 段 + $5 预算（推荐试跑）
.\.venv\Scripts\python.exe -m ink2reel_driver run `
  "..\ink2reel-evaluation\runs\inkos\export\午夜便利店_export.txt" `
  --project midnight2 --until deliverables --limit-segments 4 --budget-usd 5 --gate auto
```

| 参数 | 含义 |
| --- | --- |
| `--until assets\|storyboard\|video\|deliverables` | 媒体阶段截断点；`media` ≡ `deliverables` |
| `--limit-segments N` | 只处理剧本前 N 段（费用控制） |
| `--budget-usd X` | 预算熔断；≥80% warn，≥100% 停入队 **exit 3** |
| `--threshold X` | 六维总分下限（仅 `--gate threshold`） |
| `--no-worker` | 不启进程内 worker（假定外部已有） |

### Worker / Server 互斥

Driver 进程内启动 `GenerationWorker`（lease=`default`）。若 **uvicorn 也在跑**，两边会争同一 lease，任务可能挂起。

- **推荐**：跑媒体时停掉 server，或只用 server 的 worker 并加 `--no-worker`
- 启动时若 `127.0.0.1:1241` 有监听会打 WARN

### 全量 14 段（费用高，慎用）

```powershell
.\.venv\Scripts\python.exe -m ink2reel_driver run ... --project midnight2 --until deliverables --budget-usd 20
# 不加 --limit-segments 即全段；视频排队 5–15 分钟/段属正常，勿重复提交
```

已有 `scripts/episode_N.json` 时跳过文本链，只补媒体。

## 交付文件夹契约（codex 消费接口）

```text
deliverables/EP-001/
├── script/
│   ├── episode_script.md
│   └── storyboard.json
├── transcript/
│   ├── transcript.json
│   └── subtitles.srt
├── images/
│   ├── characters/<name>.png
│   ├── locations/<name>.png
│   └── first_frames/<segment_id>.png
├── videos/<segment_id>.mp4
├── audio/<segment_id>.wav          # 可选
├── edit_manifest.json              # status ready|missing；duration 优先 ffprobe
└── SUMMARY.md                      # 逐段首帧/视频/音频 + 成本
```

- **transcript** 时点：字数 ÷ 4.5 字/秒，最短 1.5s（段级）。
- **SRT**：句级 ≤22 字；时长 1.2–7s。
- **预算估算常数**（无 pricing 时）：图 $0.05/张、视频 $1/段、TTS $0.02/段（见 `budget.py`）。

仅导出：

```powershell
.\.venv\Scripts\python.exe -m ink2reel_driver export --project midnight2 --episode 1 --limit-segments 4
```

## 测试

```powershell
cd ink2reel
uv run pytest ink2reel_driver/tests -q
```

测试全部 mock，不打真实供应商 API。

## Patch 清单

（ArcReel 原有 `lib/` / `server/`：**无 patch**。）

## 下一棒预告

1. 质检：OnlyShot 规则 + `GatePolicy(mode=threshold)`
2. 投喂目录 `queue/` + 用户 codex 定时剪辑闭环
