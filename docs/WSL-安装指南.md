# Boujoy Harness — WSL 完整安装指南

> **适用场景**：你有一台已经用了好一阵的工作 WSL（公司账号、已有 git/node/python/dsh），想把自己 fork 的 Boujoy Harness 克隆下来跑起来用。
>
> **关键约束**：
> - 推送（push）都在 Mac 端做，WSL 端只做拉取（pull）和运行
> - 因为仓库是公开的，WSL 里的公司 SSH key 直接能 `git clone`，不需要 PAT
> - 不改动公司全局 git/ssh 配置，不污染工作环境

---

## 0. 重要提示：粘贴命令时的坑

如果你是从聊天窗口 / 网页复制命令到 WSL 终端，**千万不要把 `<br/>` HTML 标签也带进去**。
bash 会把 `<br/>` 当成"从文件 `br/` 读输入"，然后报 `No such file or directory`。

**正确做法**：每次只复制一行命令（不含任何 HTML 标签），粘到终端按回车，再粘下一行。

---

## 1. 克隆你的 fork

打开 WSL 终端（开始菜单搜 Ubuntu），逐行执行：

```bash
cd ~
```

```bash
git clone git@github.com:tyx22000044-maker/boujoy-harness.git
```

```bash
cd boujoy-harness
```

```bash
ls
```

你应该看到：
```
macos  web  windows  tests  assets  docs  boujoy-config.template.json  ...
```

再确认 commit 历史：

```bash
git log --oneline -3
```

最新一条应该是 `style(web): replace harsh punk theme with calm warm-paper design` 之类。

---

## 2. 准备知识库（Vault）

```bash
mkdir -p ~/BoujoyVault
```

```bash
cd ~/BoujoyVault
```

放一张示例笔记（没有也行，可以先空着）：

```bash
cat > "欢迎.md" << 'EOF'
# 欢迎

这是 Boujoy 的本地知识库，所有笔记都是普通 .md 文件。
EOF
```

```bash
ls
```

应该看到 `欢迎.md`。

---

## 3. 找到你的 dsh 路径

```bash
which dsh
```

应该输出类似 `/home/administrator/.nvm/versions/node/v20.10.0/bin/dsh`。然后：

```bash
DSH_ROOT=$(dirname $(dirname $(which dsh)))
echo "DSH_ROOT = $DSH_ROOT"
```

验证：

```bash
ls "$DSH_ROOT/node_modules/.bin/dsh"
```

应该能看到 `dsh` 这个文件。

把环境变量写到 `~/.bashrc`，以后每次开终端自动有：

```bash
echo "export BOUJOY_DSH_ROOT='$DSH_ROOT'" >> ~/.bashrc
echo "export BOUJOY_VAULT_DIR='$HOME/BoujoyVault'" >> ~/.bashrc
source ~/.bashrc
```

验证：

```bash
echo "DSH  = $BOUJOY_DSH_ROOT"
echo "VAULT= $BOUJOY_VAULT_DIR"
```

创建 Harness 需要的两个工作目录：

```bash
mkdir -p "$BOUJOY_DSH_ROOT/home"
mkdir -p "$BOUJOY_DSH_ROOT/clean-home"
```

---

## 4. 写启动脚本

```bash
cd ~/boujoy-harness
```

把下面这段**整块复制**到终端（不含任何 HTML 标签），按回车：

```bash
cat > start-boujoy.sh << 'EOF'
#!/bin/bash
set -euo pipefail

PORT=8766
VAULT_DIR="${BOUJOY_VAULT_DIR:?设环境变量 BOUJOY_VAULT_DIR}"
DSH_ROOT="${BOUJOY_DSH_ROOT:?设环境变量 BOUJOY_DSH_ROOT}"

mkdir -p "$VAULT_DIR" "$DSH_ROOT/home" "$DSH_ROOT/clean-home"

cleanup() {
  echo "Stopping Boujoy services..."
  for pid in $KNOWLEDGE_PID $CLEAN_PID $GATEWAY_PID; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

# 知识模式 Harness（端口 3080）
(cd "$VAULT_DIR" && BOUJOY_DSH_ROOT="$DSH_ROOT" DSH_HOME="$DSH_ROOT/home" \
  DSH_TELEMETRY_DISABLED=1 dsh web --host 127.0.0.1 --port 3080) &
KNOWLEDGE_PID=$!

# 纯净模式 Harness（端口 3081）
(cd "$HOME" && BOUJOY_DSH_ROOT="$DSH_ROOT" DSH_HOME="$DSH_ROOT/clean-home" \
  DSH_TELEMETRY_DISABLED=1 dsh web --host 127.0.0.1 --port 3081) &
CLEAN_PID=$!

# Boujoy Python 网关（端口 8766）
PYTHONDONTWRITEBYTECODE=1 python3 web/boujoy_server.py \
  --port "$PORT" \
  --vault "$VAULT_DIR" \
  --static web \
  --knowledge-home "$DSH_ROOT/home" \
  --clean-home "$DSH_ROOT/clean-home" &
GATEWAY_PID=$!

echo "Boujoy Harness is up -> http://localhost:$PORT"
echo "Press Ctrl-C to stop."
wait
EOF
```

加执行权限：

```bash
chmod +x start-boujoy.sh
```

验证文件存在：

```bash
ls -l start-boujoy.sh
```

应该看到 `-rwxr-xr-x ... start-boujoy.sh`。

---

## 5. 启动

```bash
cd ~/boujoy-harness
./start-boujoy.sh
```

会看到三组日志输出，最后一行：

```
Boujoy Harness is up -> http://localhost:8766
Press Ctrl-C to stop.
```

**保持这个终端窗口开着。** 关掉窗口或按 `Ctrl-C` 都会停掉服务。

---

## 6. 在 Windows 浏览器打开

回到 Windows，打开 Edge 或 Chrome，地址栏输入：

```
http://localhost:8766
```

WSL 会自动把 8766 端口转发给 Windows localhost，所以这个地址直接能用。

### 首次使用

- **第一次打开是深色主题**（index.html 硬编码的）。点右上角的 **◐** 按钮切到暖纸奶油浅色，会记住（存 localStorage）。
- **配置模型**：点右上角 **⌘**（命令面板） → 输入 "设置" 回车 → 切到 **模型** 标签 → 填入 DeepSeek API Key。

---

## 7. 日常操作

| 做什么 | 怎么做 |
| --- | --- |
| **启动** | 开 WSL 终端，`cd ~/boujoy-harness && ./start-boujoy.sh` |
| **停止** | 那个终端按 `Ctrl-C`，三个服务一起停 |
| **同步 Mac 端的新改动** | `cd ~/boujoy-harness && git pull`，然后重启 |
| **换知识库启动** | `BOUJOY_VAULT_DIR=/other/path ./start-boujoy.sh` |

---

## 8. 常见坑

| 现象 | 原因 & 解决 |
| --- | --- |
| `-bash: br/: No such file or directory` | 粘贴命令时把 `<br/>` 带进去了。一行一行粘，不带 HTML 标签 |
| `dsh: command not found` | PATH 里没有 dsh。跑 `export PATH="<dsh 的 bin 目录>:$PATH"` 然后写进 `~/.bashrc` |
| `Permission denied (publickey)` | 公司 SSH key 没注册到 GitHub。在 WSL 跑 `ssh -T git@github.com` 看认证身份 |
| `localhost:8766` 在 Windows 打不开 | WSL 端口转发失灵。PowerShell（管理员）跑 `wsl --shutdown`，然后重启 WSL |
| 启动后界面一直"本地引擎连接中" | Harness 启动慢，等 30 秒再刷新 |
| 删除会话/记录后找不到文件 | 移到了 WSL 的 `~/.Trash/`，不会进 Windows 回收站 |

---

## 9. 架构简述（便于排错）

WSL 里会跑三个进程：

| 端口 | 服务 | 作用 |
| --- | --- | --- |
| **3080** | 知识模式 Harness（dsh） | 工作目录 = Vault，读写知识库 |
| **3081** | 纯净模式 Harness（dsh） | 工作目录 = `$HOME`，与 Vault 隔离 |
| **8766** | Boujoy Python 网关 | 静态文件 + Vault API + WebSocket 中继 |

Windows 浏览器访问 `http://localhost:8766`，所有请求都走网关，网关再转发到两个 Harness。

---

## 10. 卸载 / 清理

不想用了：

```bash
# 删除代码（Vault 不会被动）
rm -rf ~/boujoy-harness

# 删除 Vault（谨慎！你的笔记都在里面）
rm -rf ~/BoujoyVault

# 清掉环境变量
sed -i '/BOUJOY_DSH_ROOT/d; /BOUJOY_VAULT_DIR/d' ~/.bashrc
source ~/.bashrc
```

Harness 运行时本身（`$BOUJOY_DSH_ROOT/home` 等）你装 dsh 时就有，按需处理。

---

有问题随时告诉我具体报错，我接着帮你。
