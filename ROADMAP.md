# 路线图

配合 [PRD.md](PRD.md) 看。按阶段推进，阶段之间不强制严格顺序，但阶段 2（记忆模型接入）不做完，阶段 3（MCP 自动化）的实际效果会打折扣——收件箱会一直空转在"排队等待"。

每个阶段下面的任务已经落到具体文件、具体命令、具体判断标准——不是"改一下配色"这种笼统描述。凡是我目前还没有把握的地方（比如 dsh 的 MCP 配置具体在哪个文件），标成"待调查"而不是编一个看似合理的答案。

## 总览

| 阶段 | 目标 | 状态 | 子任务数 |
|---|---|---|---|
| 0 | Boujoy + Bok 整合基础 | ✅ 已完成（2026-09-07） | — |
| 1 | 红黑设计语言落地到全部页面 | ✅ 已完成（2026-09-08，commit `67bf924`） | 6 |
| 2 | 给 Bok 接一个真正能用的记忆模型 | ✅ 已完成（2026-09-08，本地 Ollama 路线） | 4 |
| 3 | 把 bok_core 注册成 dsh 的 MCP 记忆源 | ✅ 已完成（2026-09-08，验证深度见 3.4 备注） | 4 |
| 4 | 知识库页与 Bok 工作台的收敛评估 | 未开始，观察期任务 | 3 |
| 5 | 作者自定方向 | 占位 | — |
| 6 | 分发流程刷新 | 未开始，已发现 macOS 打包会漏掉 bok_core | 3 |

---

## 阶段 0 — Boujoy + Bok 整合基础 ✅

**完成时间**：2026-09-07。已提交两次并推到 `origin/main`：`8657250`（代码）、`eac80e6`（文档）。

**改了什么**：
- `web/bok_core/`：`bok/Bok/bok_core/` 整包 vendor 过来（21 个文件，纯 stdlib，零 pip 依赖），不用 submodule/subtree
- `web/boujoy_server.py`：`BoujoyServer.__init__` 里构造一次 `BokUIBridge(vault_root=config.vault, config_overrides={"personal_core_root": "~/.bok-personal-core"})`；`do_GET`/`do_POST` 里各加一段 `/api/bok` 前缀匹配，转发给 `bridge.forward()`；新增 `_handle_bok()` 辅助方法；`main()` 的 `finally` 里加 `server.bok_bridge.close()`；顶部加了一行 `sys.path.insert(0, ...)`，因为 `tests/smoke_test.py` 用 `importlib` 而不是直接跑脚本来加载这个文件，不会自动把 `web/` 目录加进 `sys.path`
- `web/index.html`：新增桌面 `.nav-cut` + 移动端 `.mobile-nav-item` 各一个按钮，新增 `<section id="bokPage" data-page-panel="bok">`，内部两个 tab（工作台/关于我）
- `web/app.css`：新增 `.page-bok` 整段样式，`bok-` 前缀命名，复用已有的 `.section-kicker`/`.mini-cut`/`.search-cut`/`.empty-records` 等共享类
- `web/app.js`：`PAGE_META` 加 `bok` 项，`showPage()` 加一行分支，新增 `bokFetch`/`loadBok`/`renderBokWorkbench`/`renderBokPerson`/`searchBok`/`saveBokQuickNote`/`bokAction`/`setBokTab` 一组函数，`bindEvents()` 里加委托点击分支和几个直接监听
- `.gitignore`：加了一条 `/.dev-vault/`

**过程中发现并修的两个 bug**：
1. `_handle_bok` 一开始用 `dict(self.headers)` 转发请求头，把 Python `email.message.Message` 的大小写不敏感查找能力丢了——`ui_bridge.py` 按精确大小写 `"Content-Type"` 取值，取不到就不带这个头转发给 `bok_core`，导致所有 POST 请求（保存速记等）报 415。改成直接用 `self.headers.get("Content-Type")`/`self.headers.get("Idempotency-Key")` 显式取这两个头。
2. 写新 `.gitignore` 时用 `Write` 直接覆盖，没意识到这个仓库里 `.gitignore` 虽然在 git 历史里有真实内容，但工作目录里这一份是缺失的（跟外层 XU4N Harness 仓库最初被发现的"文件夹看着空、git 记录还在"是同一类问题）——覆盖丢了 `.env`/`*.pem`/`*.key`/`vault/`/`runtime/` 这些安全相关规则，已经用 `git show HEAD:.gitignore` 找回原内容，合并了新加的那一条。

**验证方式**：起了本地开发服务器（`.claude/launch.json` 里的 `boujoy-dev` 配置，指向 `boujoy-harness/.dev-vault`），在浏览器里逐项点过：Bok 页面打开、搜索命中真实 vault 内容、保存速记后落盘成 Markdown 文件且 Boujoy 自己的知识库页面也能看到、点"整理为记忆"后端返回 200（转化本身因为没配 provider 会排队，属预期行为）、Personal Core 面板正确显示"已配置但暂无画像"。另外跑了 `python3 tests/smoke_test.py --skip-live`，16 项非实时检查全过，确认原有六个页面没有回归。

---

## 阶段 1 — 红黑设计语言落地到全部页面

**✅ 完成记录（2026-09-08，commit `67bf924`）**：`web/app.css` 全量迁移到新 token 系统——`:root`/深色块按第二节色值重写，旧名字（`--ink`/`--paper`/`--paper-2`/`--panel-solid`/`--blue`/`--acid`/`--pink`/`--cyan`/`--muted`）全部改名收敛，级联生效；1.2 硬编码清理完成（`--danger`/`--on-accent`/`--scrim`/`--scene-*`/`--video-backdrop` 提为正式 token，`:root` 块外已无裸 hex）；1.3 选方向 B——`--cat-1..4` 低饱和色阶（蓝灰/青灰/紫灰/橄榄灰）专用于知识卡 kind 图标、新闻双栏、专家头像、监控卡顶条、图谱节点等静态分类，红只留给交互/激活/强调；`--success`/`--warn` 保留状态三灯（就绪/连接中/出错）；1.5 阴影收为单层浅投影，`--shadow-lg` 只留给弹窗/菜单/toast/断连横幅；圆角补 `--r-xs`/`--r-pill`，徽标类改药丸、小按钮统一 `--r-xs`；焦点环改红（键盘 Tab 实测 `rgb(225,54,43)`）。验收按 1.6 做完：无头 Chrome 对 7 个页面各截浅色/深色共 16 图逐张目检，专家/风格页用临时造数验证了带卡片状态（造数已删）。图谱 SVG 节点色与 manifest 主题色同步更新。

（迁移前状态：只有 07 Bok 页面按 [DESIGN-LANGUAGE.md](DESIGN-LANGUAGE.md) 写，其余六页是老的暖色系配色。）`web/app.css` 里查过了实际的 token 使用量：`--blue` 114 处、`--cyan` 34 处、`--pink` 27 处、`--acid` 22 处引用。**改 `:root` 里的定义值就能级联生效，不需要逐个改这 197 处引用**——真正需要手动处理的是下面 1.2、1.3 两类"绕开了 token 系统"的地方。

### 1.1 `:root` token 重写（浅色 + 深色两套）

完整的现有 token 清单（比 DESIGN-LANGUAGE.md 第六节列的更全，那份文档只挑了核心几个）：

| 现有 token | 浅色现值 | 深色现值 | 处理方式 |
|---|---|---|---|
| `--ink` | `#2a2620` | `#f5f0e3` | 改成新 `--text`（`#161616` / `#F2F2F2`）；评估能否和下面的 `--text` 合并成一个 token，现在两个高度重叠的深色文字变量同时存在，是历史遗留 |
| `--text` | `#38332b` | `#f2ede0` | 同上，收敛到 `--text` |
| `--muted` | `#877f70` | `#b6afa1` | 改成 `--text-muted`（`#6E6E6E` / `#9A9A9A`） |
| `--paper` | `#faf7f1` | `#141519` | 改成 `--bg`（`#F7F6F2` / `#0A0A0A`）——深色这一步是本次改动里色值跳动最大的一个，从偏蓝的 `#141519` 换成接近纯黑 |
| `--paper-2` | `#fffdf9` | `#1c1e24` | 改成 `--surface`（`#FFFFFF` / `#161616`） |
| `--panel-solid` | `#fffdf9` | `#22252c` | 同 `--surface`，评估能否和 `--paper-2` 合并 |
| `--blue`（当前唯一的"主操作色"，焦点环也用它） | `#4a6b9f` | `#9cb8e2` | 改成新 `--red`（`#E1362B` / `#FF4433`）——**这是核心改动**，主按钮、激活态、`:focus-visible` 描边全部从蓝变红 |
| `--line` | `rgba(58,50,38,.14)` | `rgba(240,234,220,.19)` | 改成实色 `--line`（`#E8E5E0` / `#2A2A2A`），边框存在感更明确，不再是半透明叠加 |
| `--acid`（当前 22 处，含"在线"状态点） | `#5e8c61` | `#a6c79c` | 状态语义（`.service-pill.online`）保留为新 `--success`（`#1E9E5C` / `#33C67D`）；非状态语义（知识卡片"知识"类图标等，见 1.3）需要单独决策 |
| `--yellow`（"连接中"状态点，之前设计语言文档漏掉了这个 token） | `#c08a2d` | `#e0b875` | 保留，作为第三个状态色（就绪=绿、出错=红、连接中=黄），这条不受"收敛强调色"约束——状态指示灯是功能性信号，不是品牌强调色 |
| `--pink` / `--cyan`（当前分别 27 / 34 处，主要用途见 1.3） | `#b56a86` / `#4f8f8a` | `#d7a4b8` / `#93c8c1` | 不作为独立品牌色保留；用途拆解见 1.3 |
| `--r-sm` / `--r-md` / `--r-lg` | `6 / 10 / 14px` | 同 | 微调到 `8 / 10–12 / 14–16px`，量级不用大改 |
| `--shadow` / `--shadow-lg` | 双层深阴影 | 双层深阴影 | 见 1.5 |

### 1.2 硬编码色值清理（绕开 token、需要逐个改的）

`app.css` 里搜了一遍 `:root` 块以外的裸十六进制色值，主要是这几组：

- **`#c4574e`（浅色）/ `#e06c60`（深色）—"错误/危险"语义**，硬编码在至少 7 处：`.service-pill.error i`、`.text-tool.danger`（深色）、`.toast.error`（深色）、`#pairError`（深色）、`.stop-button` 及其 hover（深色）。这组颜色本来就已经是红色系，跟新主红 `#E1362B` 色相接近——**改动量小**，建议提成一个正式的 `--danger` token（比如就用 `--red-strong`，或单独定一个比主红更暗一档的红以区分"这是错误"和"这是品牌强调"），把这 7 处硬编码换成引用这个 token，而不是继续散落着写死的十六进制。
- **`#fffdf9`（"强调色背景上的白字"）**，硬编码在按钮 hover、`.brand-mark` 等多处，本质是 `--paper-2`/`--surface` 的重复。改成显式的 `--on-accent: #FFFFFF` token，语义更清楚（"画在强调色色块上的文字颜色"，不是"页面背景色"）。
- **深色模式专用的裸色值**（`#2c3038`、`#181b21`、`#14161b`、`#121317` 一类）：需要找到具体用在哪（大概率是骨架屏渐变、特定卡片的深色背景变体），逐个确认是否该收敛进 `--surface`/`--line` 的深色值，还是有理由保留独立值（比如骨架屏渐变本来就需要跟背景有细微差别，不适合直接等于 `--surface`）。
- **暖色系装饰性渐变**（`#f8f2e6`/`#f2ecdf`/`#eee6d6` 一类，推测是骨架屏 `linear-gradient` 里的中间色）：跟着 `--bg`/`--surface` 的新值重新调一版暖度更低的灰渐变。

### 1.3 多色语义系统的重新设计（需要一个明确决策，不是纯执行任务）

发现两处依赖"多个高饱和强调色区分类别"的地方，跟"只留一个红作强调色"的原则冲突，需要在动手前决定怎么处理：

1. **知识卡片的"kind"图标配色**（`app.css:392-395`）：`project`→`--blue`、`knowledge`→`--cyan`、`content`→`--pink`，其余类别（提示词/商业/Skills/系统/其他）落到默认的 `--acid`。四种色相区分七种内容类别。
2. **新闻页的两栏配色**（News 页"AI 实时新闻"栏用 `--blue`，"工具与模型动向"栏用 `--cyan`，边框/来源标签同色系区分）。

两处本质是同一个问题："用色相区分类别"这个模式在只有一个品牌红的新语言里要怎么办。两个可选方向，选一个即可，不用两处分别决策：

- **方向 A（推荐）**：类别区分改成"图标本身的图形差异 + 统一的中性底色（浅灰或白底黑图标）"，红色继续只用在"这是可点击的/激活的/新的"这类交互语义上，不用来区分静态类别。彻底贯彻"一个强调色"的原则，但要重新设计七个 kind 图标的具体图形，工作量在图标设计上。
- **方向 B**：保留一组**低饱和度**的中性色阶（不是现在这种高饱和的蓝/青/粉）专门用于类别区分，跟品牌红明确拉开辨识度差异（红=强调，灰阶冷色=分类标签）。改动量更小（只是把现有色相降饱和度），但没有 A 彻底。

### 1.4 逐页检查清单

| 页面 | 具体要看的地方 |
|---|---|
| Agent（01） | 消息气泡边框、`composer` 输入框焦点环（当前用 `--blue`）、发送按钮、审批/打断卡片的强调色 |
| Knowledge（02） | 1.3 的 kind 图标决策落地、搜索框焦点态、"打开上下文"链接色 |
| Experts / Styles（03/04） | `.collection-hero`、`.giant-add` 按钮、`.record-card` 的 hover 边框色 |
| Monitor（05） | `cache-hit-fill` 进度条、`context-meter`（这两处现在什么颜色需要单独确认，之前没细看） |
| News（06） | 1.3 的两栏配色决策落地 |
| Bok（07） | 已经是新语言，作为其余六页的参照标准，不用改，但等 1.1-1.3 做完后回头对比一下是否完全一致 |

### 1.5 阴影收轻

现有 `--shadow`/`--shadow-lg` 是双层深阴影（`0 1px 2px ... , 0 6px 20px ...` 和 `0 2px 4px ..., 0 14px 38px ...`）。参考截图里的卡片层次更多靠 `--line` 边框，阴影只是弱提示。建议改成单层、更浅：`--shadow: 0 1px 2px rgba(0,0,0,.04)`，`--shadow-lg` 保留给弹窗/下拉这类真正悬浮的元素，卡片本身不用 `--shadow-lg`。

### 1.6 验收标准

- 六个老页面 + Bok 页面每个截一张浅色、一张深色，人工过一遍，确认没有页面因为共享 token 改动而"崩版"（对比度不够、看不清文字之类）
- `:focus-visible` 焦点环从蓝改红后，用 Tab 键走一遍主要交互元素，确认可见性没退化
- 1.2 里提到的硬编码色值改完后，全局搜索确认 `app.css` 里不再有裸十六进制色值散落在 `:root` 块以外（骨架屏渐变这类确实需要独立值的除外，但要有 token 命名，不是直接写死）

---

## 阶段 2 — 给 Bok 接一个真正能用的记忆模型

**✅ 完成记录（2026-09-08，本地 Ollama 路线）**：本机 Ollama 0.33.2 已装好且服务在跑（`127.0.0.1:11434`）。拉取并采用 `qwen2.5:7b-instruct`（4.7GB，M1 Pro 32GB 无压力，结构化 JSON 输出实测合规：对 `bok_core` 的 `OLLAMA_MEMORY_SCHEMA` 一次通过，冷启动首 token 约 45-50 秒、加载后单条约 20 秒，不需要退化到 `llama3.2:3b`）。`.dev-vault/.bok/config.json` 写入：

```json
{
  "provider": "auto",
  "provider_model": "qwen2.5:7b-instruct",
  "auto_start_local_model": true
}
```

显式钉 `provider_model` 是有意的：`provider.py` 的 `_discover_ollama_model()` 在没指定模型时取 `/api/tags` 返回的第一个（不保证是想要的那个），钉住后不依赖顺序。接入前用与 `generate_json` 完全相同的请求体直测过一次模型输出（合法 JSON、全字段齐、source_excerpt 逐字命中）。2.4 验收按序做完：保存真实语义速记（"macOS 分发一律免安装便携包"这条偏好）→ promote → 约 50 秒后（30 秒批处理窗口 + 模型推理）记忆收件箱产出真实候选，`analysis.summary` 准确复述决定、`memory_type=decision`、`requires_review=True`，Bok 工作台 UI 肉眼确认。全程零云端 API、零付费。

现在本机没配置 Ollama 也没配置任何 provider，记忆收件箱会一直停在"排队等待"，Personal Core 也不会真正形成理解。（← 这段是迁移前的旧现状，保留备查；已完成见上。）

### 2.1 路线决策

`bok_core` 的 `provider` 字段（`config.json` 里）支持三种：

| 路线 | 优点 | 缺点 | 配置方式 |
|---|---|---|---|
| 本地 Ollama（`provider="auto"` 或 `"ollama"`） | 完全本地、零成本、符合 Bok "local-first" 的设计初衷 | 需要本机能跑得动一个能稳定输出结构化 JSON 的模型，响应速度取决于硬件 | 装 Ollama + 拉模型，见 2.2 |
| 云端 OpenAI 兼容 API（`provider_base_url`+`provider_api_key_ref`） | 不挑硬件、响应快、模型能力上限更高 | 记忆判定这一步的内容会发到第三方，跟"本地优先"的产品定位有一定张力；有调用成本 | 见 2.3 |
| 两者都配，本地优先云端兜底 | 平时本地跑，本地探测不到时不丢功能 | 配置复杂度最高，需要验证两条路径都真的工作 | 2.2 + 2.3 都做，`provider` 留 `"auto"` |

### 2.2 如果选 Ollama

1. 装 Ollama（`brew install ollama`，或从官网下载）
2. 拉一个支持结构化输出、参数量适合本机跑的模型（具体选哪个型号取决于本机显存/内存，需要实际测试而不是我在这里指定一个型号）
3. `bok_core` 默认 `auto_start_local_model=true`，探测 `127.0.0.1:11434`，正常情况装完不用额外配置这一步
4. 如果想手动指定模型，在 vault 的 `.bok/config.json` 里加 `"provider_model": "<模型名>"`

### 2.3 如果选云端 API

在 vault 的 `.bok/config.json` 里配：
```json
{
  "provider": "openai_compatible",
  "provider_model": "<模型名>",
  "provider_base_url": "<API 地址>",
  "provider_api_key_ref": "env:BOK_PROVIDER_API_KEY"
}
```
`provider_api_key_ref` 只接受 `env:`（读环境变量）或 `keychain:`（读系统钥匙串）引用，**不要把 key 明文写进这个文件**——这个文件本身在 vault 里，vault 目录已经在 `.gitignore` 里，但明文 key 落盘本身仍然是要避免的做法。

### 2.4 验证方法

1. 在 Bok 工作台里保存一条包含明确、具体偏好陈述的速记（不要用"测试"这种没有实际语义的内容，不然模型判定也无法真正验证）
2. `bok_core` 的批处理窗口是"攒够 10-20 条或空闲约 30 秒"触发一次模型分析（见 `bok_core/api.py`），所以不会立刻出结果，等半分钟左右
3. 刷新记忆收件箱，确认状态从"排队等待"变成了真的产出候选（有 `analysis.summary`/`analysis.reason` 内容，不是空的）
4. 如果一直卡在等待，检查 Ollama 是否真的在跑（`ollama list`）、或云端 API key 是否有效

---

## 阶段 3 — 把 bok_core 注册成 dsh 的 MCP 记忆源

**✅ 完成记录（2026-09-08）**：注册已写入 `~/.dsh/profiles/web/cordis.patch.yml`（整条替换掉指向已删除的 xu4n_memory 的失效旧注册，避免 dsh 每次启动 spawn 不存在的模块）；内容见 3.2。验证链路与深度见 3.4（含一处"哪一步验证是靠云端已有会话跑完的"如实备注）。

现在只能在 Bok 页面手动搜索/速记，Agent 对话本身不会自动读写记忆。（← 旧现状，保留备查）

### 3.1 dsh 的 MCP 注册方式在哪（已查清）

调查结论（2026-09-08，不是猜测，逐条实测）：

1. **注册入口**：`~/.dsh/profiles/web/cordis.patch.yml` —— dsh 每个 profile 的 patch 层，顶层是一个 YAML 数组。
2. **承载插件**：`@deepseek-ai/dsh-mcp-client`（已随 dsh 运行时安装在 `~/.dsh/profiles/node_modules/@deepseek-ai/dsh-mcp-client`，`package.json` 描述即 "connects to MCP servers and registers their tools on ctx.tools"）。README 里 `config` 支持 `transport: stdio` 或 `streamable-http`，stdio 需要 `command`/`args`/`env`/`cwd`。
3. **必须用 `- insert:` 包裹**：裸的 `- id: ... name: ... config: ...` 会被 patch 引擎（`cordis-plugin-include` 的 `applyEntryPatches`）当成"对某个已存在的 base 层条目做覆盖"，找不到目标 id 时以 `patch: entry "<id>" not found` 报错。新增插件必须写成 `- insert:\n    - {id, name, config}`。这是上一轮接入 xu4n_memory 时踩过并记录在旧 `docs/DSH_VERSION.md` 里的坑，本次沿用。
4. **工具命名**：注册后模型看到的是 `mcp__<serverName>__<tool>`，例如 `serverName: bok` + `bok_search` → `mcp__bok__bok_search`（Claude Code / Codex 同款命名形态）。
5. **环境清洗**：stdio 子进程用 `scrubbedParentEnv()`（删除匹配 `/KEY|PASSWORD|SECRET|TOKEN/i` 和所有 `DSH_*` 的环境变量）叠加配置里的 `env`，所以我们在 `env` 里显式给的 `PYTHONPATH` 会保留。
6. **旧的失效注册要一并清掉**：`cordis.patch.yml` 里原先有一条 `mcp-xu4n-memory`（`serverName: xu4n-memory`，指向本仓库外层 XU4N Harness 已删除的 `packages/memory-engine` 和 `Vault`）。留着它，harness 每次启动都会尝试 spawn 一个已经不存在的 `python3 -m xu4n_memory`（`failOnStartupError` 默认 false，不会崩，但日志里会持续报错、工具全部调用失败）。本次把它整条替换成 bok 的注册，而不是并存。

### 3.2 注册配置（实际写入 `~/.dsh/profiles/web/cordis.patch.yml` 的内容）

`command/args` 保持 ROADMAP 原定三字段，`env.PYTHONPATH` 指向 `web/`（`bok_core` 的父目录），`cwd` 可选（给了 PYTHONPATH 后不再依赖 cwd）：
```json
{
  "command": "python3",
  "args": ["-m", "bok_core", "--vault", "<boujoy vault 的绝对路径>", "mcp"],
  "env": { "PYTHONPATH": "<boujoy-harness/web 的绝对路径>" }
}
```
`PYTHONPATH` 要指向 `web/` 这一级（`bok_core` 所在目录的父级），不是指向 `bok_core` 本身。

实际落盘的完整条目（开发/验证期 vault 为 `boujoy-harness/.dev-vault`，接入真实 vault 时替换 `--vault` 后的路径即可；`serverName: bok` 使工具呈现为 `mcp__bok__bok_*`）：
```yaml
- insert:
    - id: mcp-bok-memory
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: bok
        transport: stdio
        command: python3
        args: ['-m', 'bok_core', '--vault', '/Volumes/ExtraStorage/XU4N Harness/boujoy-harness/.dev-vault', 'mcp']
        env:
          PYTHONPATH: '/Volumes/ExtraStorage/XU4N Harness/boujoy-harness/web'
```

### 3.3 十个工具逐个分级

**落地情况（2026-09-08 查清）**：dsh 侧目前**没有**"每个工具一个信任档位"的声明式配置——`dsh-mcp-client` 的 config 只有连接参数（transport/serverName/command/args/env/reconnect 等），权限粒度是会话级的（新会话默认权限模式 + 会话内 `/permission` 弹窗，见 `dsh-client-ui-permission-presets`），粒度不到 `mcp__bok__bok_*` 单个工具。所以下表现阶段是"使用时的预期与观察基准"，不是能直接写进某个配置文件的东西；真正的安全兜底在 bok_core 服务端自己：capture 一律排队不直接改动、important 类型必须过人工收件箱、`mcp.py` 的 `instructions` 会提示模型"先只读、重要记忆需审阅"。观察期若发现模型默认行为不符合下面某档，再决定要不要给 dsh 提需求或在本机换 preset。

`bok_core/mcp.py` 里暴露的十个工具，按"是否需要经过审批"分三档（这是建议，最终由使用时的实际体验决定要不要调整）：

| 工具 | 性质 | 建议信任级别 |
|---|---|---|
| `bok_search` / `bok_context` / `bok_project_resume` / `bok_person_context` | 只读查询 | 直接放行，不需要审批 |
| `bok_capture_memory` / `bok_quick_note` | 写入，但走的是"先排队、后台模型判定"的安全路径，不会立即改动重要内容 | 直接放行 |
| `bok_observe_conversation` | 把整轮对话内容喂给 Bok 做后台分析 | 需要决定：这是否等同于把每轮对话都发给记忆管线，如果本机模型/云端 API 有隐私顾虑，这一条可能需要用户显式开关，而不是默认开启 |
| `bok_record_person_impact` / `bok_record_person_outcome` | 直接影响 Personal Core 画像的强度/权重 | 建议过审批，这类操作会累积性地改变"Bok 怎么理解你"，出错的代价比普通记忆卡片高 |
| `bok_memory_inbox` | 查询收件箱列表 | 只读，直接放行 |

### 3.4 验证方法

跑一段真实的 dsh 对话（不是测试性对话），结束后打开 Bok 工作台的记忆收件箱，确认出现了跟这段对话相关、内容对得上的候选——不是随便出现了什么候选就算数。

**✅ 实际验证结果（2026-09-08，含验证边界，如实记录）**：

已验证到的：
1. **bok_core 的 MCP 服务器本身**（独立 stdio，不经 dsh）：`initialize` → `tools/list` 列出全部十个工具（`bok_search`/`bok_context`/`bok_project_resume`/`bok_person_context`/`bok_capture_memory`/`bok_observe_conversation`/`bok_record_person_impact`/`bok_record_person_outcome`/`bok_quick_note`/`bok_memory_inbox`）；`tools/call bok_quick_note` 真的在 `.dev-vault/07-Quick-Notes/` 落了盘、`tools/call bok_search` 命中真实内容。纯 Python，无云端。
2. **dsh 侧注册生效**：headless Chrome 驱动 dsh 原生 web UI，`provider.py` 的模型选择器换到 `qwen2.5:7b-instruct`（本地 Ollama）后开新会话，Agent 轨迹里真实出现了 `工具调用 mcp__bok__bok_search · default`，并正确复述出阶段 2 存的那条"macOS 免安装便携包"决定的原文和文件路径；第二轮 `mcp__bok__bok_quick_note` 落盘。证明注册→发现→命名→调用全链路对。
3. **capture 端到端进收件箱**：一段 dsh 对话里 Agent 调 `mcp__bok__bok_capture_memory`（source 标 `type:"mcp"`，ref "deepseek-harness session"），约一分钟后 Bok 工作台记忆收件箱出现 `decision` 类候选，summary "Boujoy 新闻页抓取策略确定，包括启动时间、抓取频率及失败处理方式" 与那句对话内容逐字对得上。**注意**：跑这条 capture 的那次 dsh 会话，模型面板显示为 DeepSeek-V4-Flash（一个此前已存在的会话，dsh 会话级模型记忆所致）——即该 capture 的"生成记忆"这一步的推理提供方是云端；但被驱动的注册项、工具发现、落盘、批处理与收件箱判定逻辑都是本地链路，且阶段 2 已单独证明本地模型能独立完成同样的分析产出候选。

没完全验证到的 / 已知问题：
- 想专跑一次"本地 qwen 模型驱动的 dsh 会话 → capture → 收件箱"三连时，两次被 dsh 前端在重负载 headless 下渲染进程崩溃打断（`Target closed`），未拿到第三次干净复跑。判定：链路各段均已分别证实（工具可被本地模型调用=第 2 条；capture→收件箱闭环=第 3 条；本地模型可独立产出候选=阶段 2），组合缺一次同框复现，非功能缺口。
- 环境备注：dsh 的模型走 `ollama-local`（openai-completions 兼容层），该 provider 在 `~/.dsh/settings.yaml` 里配 `apiKeyEnv: OLLAMA_API_KEY`；Ollama 不校验 key，启动 dsh 时注入本地哑值即可，不涉及任何云端付费 API。这一步是 dsh 侧的 provider 配置，不是 Bok 记忆模型的配置（后者保持 `local_only`、零云端）。

---

## 阶段 4 — 知识库页与 Bok 工作台的收敛评估

观察期任务，明确不在阶段 1-3 做完之前动手改。

### 4.1 观察指标
实际使用一段时间后，记录：两个页面各自的打开频率、每次打开分别是为了什么场景（快速看一眼最近文件 vs 搜索/审核记忆/查画像）。

### 4.2 判断标准
如果两个页面的使用场景高度重叠（比如打开知识库页纯粹是为了搜索，而 Bok 的搜索已经能覆盖这个需求），倾向合并；如果知识库页承担的是"文件浏览器"这种 Bok 工作台不管的角色，倾向保留两个页面。

### 4.3 如果决定合并，大致技术路径（先列选项，不展开设计）
- 方案一：知识库页保留文件浏览/反链图谱这类 Bok 没有的能力，把它的全文搜索入口换成调用 Bok 的 `/v1/search`（语义更好），两个后端接口合并成一个前端搜索框
- 方案二：两个页面维持独立后端，只在导航层面合并成一个页面里的两个 tab（类似现在 Bok 页面内部"工作台/关于我"的做法）

---

## 阶段 5 — 作者自定方向

占位阶段。以后往这里加条目时，按这个结构写，保持和前面几个阶段一样的细化程度，不要退化成一行一句话：

```
### 5.N — <标题>

**目标**：<解决什么问题/为什么要做>

**任务**：
1. ...
2. ...

**验收标准**：<怎么算做完了>
```

---

## 阶段 6 — 分发流程刷新

### 6.1 macOS 打包 — 已确认的 bug

`macos/build-app.command` 第 97-98 行是逐个文件名列出来 `cp` 的：
```
/bin/cp "${PROJECT_DIR}/web/index.html" "${PROJECT_DIR}/web/app.css" "${PROJECT_DIR}/web/app.js" "${PROJECT_DIR}/web/boujoy_server.py" "${RESOURCES}/"
```
**这里不包含 `web/bok_core/`**，也就是说现在如果直接跑一次 macOS 打包，产物里的 `boujoy_server.py` 会在启动时 `ModuleNotFoundError: No module named 'bok_core'`——这不是"需要验证"的疑虑，是能直接从脚本内容确认的真实缺口。修法是加一行：
```
/bin/cp -R "${PROJECT_DIR}/web/bok_core" "${RESOURCES}/"
```
（用 `-R` 因为这是目录，不是单个文件；插入位置紧跟在现有那两行 `cp` 后面。）

### 6.2 Windows 打包 — 已确认没问题，但要实测

`windows/Build-Windows-Portable.ps1` 第 35 行是整个 `web/` 目录 `-Recurse` 拷贝的，`web/bok_core/` 会自动包含进去，不需要改代码。但要注意：这条路径没在真实 Windows 机器上跑过（`WINDOWS-RELEASE-STATUS.md` 本来就标注 Beta），需要找一台实际的 Windows 机器跑一次完整打包+启动，确认 Python 侧（`bok_core` 是纯 stdlib，理论上跨平台没问题，但没有实测过 Windows 上 `bok_core/storage.py` 里 `msvcrt` 那条文件锁分支）。

### 6.3 `privacy_audit` 移植

Bok 原仓库有 `privacy_audit.py`，发布打包前扫描输出目录里的私钥模式、OpenAI 风格 key、macOS/Windows 用户路径泄漏。Boujoy 自己的 `RELEASING.md` 目前没有这类自动检查。评估要不要把这个脚本移植进 `boujoy-harness`（路径和检查规则需要按 Boujoy 自己的打包产物结构调整，不能直接照搬），接进 `RELEASING.md` 描述的发布流程里，作为打包脚本跑完之后的一步。
