# 路线图

配合 [PRD.md](PRD.md) 看。按阶段推进，阶段之间不强制严格顺序，但阶段 2（记忆模型接入）不做完，阶段 3（MCP 自动化）的实际效果会打折扣——收件箱会一直空转在"排队等待"。

## 总览

| 阶段 | 目标 | 状态 |
|---|---|---|
| 0 | Boujoy + Bok 整合基础 | ✅ 已完成（2026-09-07） |
| 1 | 红黑设计语言落地到全部页面 | 未开始 |
| 2 | 给 Bok 接一个真正能用的记忆模型 | 未开始，阻塞阶段 3 的实际效果 |
| 3 | 把 bok_core 注册成 dsh 的 MCP 记忆源 | 未开始 |
| 4 | 知识库页与 Bok 工作台的收敛评估 | 未开始，观察期任务，不是马上做 |
| 5 | 作者自定方向 | 占位，内容待定 |
| 6 | 分发流程刷新（把 bok_core 纳入便携打包） | 未开始，优先级最低 |

---

## 阶段 0 — Boujoy + Bok 整合基础 ✅

**完成时间**：2026-09-07。

- 克隆 `boujoy-harness`、`bok` 两个仓库到本地（配了 GitHub 专用 SSH key）
- 研究清楚两者的真实关系（Bok 的 UI 层本就是 Boujoy 分支出去的，不是两个独立产品）
- 定下整合方案：以 Boujoy 现有的 dsh 套壳为基座，Bok 的记忆引擎作为界面里的子功能接入，不做两个仓库的字面代码合并
- `bok_core` vendor 进 `web/bok_core/`，通过 `BokUIBridge` 挂进 `boujoy_server.py`，新增 `/api/bok/*` 路由
- 新增 07「Bok」页面：工作台（搜索/速记/记忆收件箱）+ 关于我（Personal Core 画像）
- 本地起服务实测：真实读写 vault、搜索命中真实内容、审批/采用动作全链路走通；跑过一遍 `tests/smoke_test.py`（16/16 非实时检查通过），确认原有六个页面没有回归
- 过程中发现并修复两个 bug：请求头大小写不敏感性丢失导致的 415、`.gitignore` 误覆盖（已恢复原有的安全相关忽略规则）

---

## 阶段 1 — 红黑设计语言落地到全部页面

**目标**：现在只有 Bok 页面是按新设计语言写的，其余六个页面还是老的暖色系配色。把 [DESIGN-LANGUAGE.md](DESIGN-LANGUAGE.md) 里定的 token 应用到 `web/app.css` 的 `:root`，让整个产品视觉统一。

| 任务 | 说明 |
|---|---|
| 替换 `:root` 色彩 token | 按 DESIGN-LANGUAGE.md 第六节的映射表逐个改，浅色+深色两套都要改 |
| 收敛强调色 | 现有 `--pink`/`--acid`/`--cyan` 分散用在不同页面，逐个改成统一用 `--red` + `--success` |
| 检查圆角分配 | 排查现有按钮/卡片/徽标有没有"该用直角矩形却用了药丸，或反过来"的地方，按 DESIGN-LANGUAGE.md 第三节的判定口诀修 |
| 阴影收轻 | `--shadow`/`--shadow-lg` 两层深阴影改成更轻的单层，卡片层次更多靠 `--line` 边框 |
| 视觉回归检查 | 六个老页面 + Bok 页面截图过一遍，确认没有页面因为改了共享 token 而崩版式 |

不涉及功能/交互改动，纯视觉替换，做完应该看不出来哪里"坏了"，只会看出来配色变了。

---

## 阶段 2 — 给 Bok 接一个真正能用的记忆模型

**目标**：目前本机没有配置 Ollama 也没有配置任何 provider API key，记忆收件箱里的候选会一直停在"排队等待"，Personal Core 也不会真正形成理解。这是当前 Bok 功能"看起来能用但实际不产出结果"的根本原因。

| 任务 | 说明 |
|---|---|
| 决定用本地模型还是云端 API | `bok_core` 的 `provider="auto"` 默认探测本机 Ollama（`127.0.0.1:11434`），也支持 OpenAI 兼容的 BYOK。二选一，或者两个都配（本地优先，云端兜底） |
| 如果选本地模型 | 装 Ollama，拉一个能稳定输出 JSON 的模型；`auto_start_local_model` 默认开着，装好基本免配置 |
| 如果选云端 API | 在 `~/.bok-personal-core` 同级的 vault `.bok/config.json` 里配 `provider_model`/`provider_base_url`/`provider_api_key_ref`（`env:`或`keychain:`引用，不要明文写 key） |
| 验证 | 保存一条速记 → 观察记忆收件箱是否真的从"排队等待"变成产出候选，而不是一直空转 |

---

## 阶段 3 — 把 bok_core 注册成 dsh 的 MCP 记忆源

**目标**：现在只能在 Bok 页面手动搜索/速记，Agent 对话本身不会自动读写记忆。`bok_core` 已经自带 10 个 MCP 工具（`bok_search`/`bok_context`/`bok_capture_memory`/`bok_observe_conversation`/… 见 `web/bok_core/mcp.py`），接进 dsh 之后对话过程会自动积累记忆、自动带上下文。

| 任务 | 说明 |
|---|---|
| 确认 dsh 的 MCP 注册方式 | 参照 `bok_core` 自带的 `mcp-config.example.json` 写法（`python3 -m bok_core --vault <path> mcp`），确认怎么接进当前 dsh 的 profile 配置 |
| 决定权限边界 | `bok_observe_conversation` 会让 dsh 把每轮对话内容喂给 Bok 做后台分析——要不要经过审批队列，还是默认信任本机 MCP 工具，需要定一下 |
| 依赖阶段 2 | 没有可用的记忆模型，接了 MCP 也只是把候选堆进收件箱，看不出效果，建议阶段 2 先做完 |
| 验证 | 跑一段真实对话，确认结束后记忆收件箱里出现了跟这段对话相关的候选 |

---

## 阶段 4 — 知识库页与 Bok 工作台的收敛评估

**目标**：现在 Boujoy 原生的知识库页（02）和 Bok 工作台（07）并存，两套 vault 浏览体验有一定重叠。这是个观察期任务，不是现在就要做的事——先用一段时间，看两个页面的实际使用频率和场景差异，再决定要不要合并、怎么合并。不要在阶段 1-3 做完之前提前动手改这块。

---

## 阶段 5 — 作者自定方向

占位阶段。整合基础打好之后，"自己再来改"的具体内容——新功能、新页面、新交互——留在这里逐步补充，暂无固定条目。

---

## 阶段 6 — 分发流程刷新

**目标**：`bok_core` 现在是新加的 vendor 依赖，`macos/make-runtime-portable.command` 之类的便携打包脚本还没验证过带着 `bok_core` 一起打包是否正常（比如路径重写逻辑会不会漏掉 `web/bok_core/`）。优先级最低，等前面几个阶段稳定、真的要出一个便携包分发出去时再做。

| 任务 | 说明 |
|---|---|
| 便携打包验证 | 跑一次 `build-app.command` / `make-runtime-portable.command`，确认打包产物里 `web/bok_core/` 完整、路径重写没有遗漏 |
| Windows 侧验证 | Windows 支持本来就标注 Beta，加了 Bok 之后需要重新跑一遍 `Build-Windows-Portable.ps1` 确认 |
| `privacy_audit` 式检查 | Bok 自己原有仓库里有一个发布前扫描私钥/用户路径泄漏的脚本（`privacy_audit.py`），评估要不要把这个检查也接进 Boujoy 的 `RELEASING.md` 流程 |
