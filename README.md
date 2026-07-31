# VibeAI Sub2API 图像 Skill

这是一个供 Codex 使用的图像生成与编辑 Skill。安装并配置一次后，可以直接在 Codex 对话中描述图片需求，不需要手动调用接口或安装第三方 Python 包。

## 安装前准备

- 已安装并可以正常使用的 Codex
- Python 3.10 或更高版本
- 一个已启用图像生成功能的 Sub2API API Key
- 使用 Git 下载或更新时需要 Git；Windows 首次安装也可以直接下载 ZIP

## Windows 安装

### 最简单：双击安装

1. 在 GitHub 仓库页面选择 **Code > Download ZIP**。
2. 解压下载的 ZIP。
3. 双击仓库根目录中的 `install.bat`。
4. 根据窗口提示输入 Base URL 和 API Key，安装完成后按任意键关闭窗口。

`install.bat` 会依次查找 `py -3`、`python` 和 `python3`，自动选择 Python 3.10 或更高版本，不受 PowerShell 脚本执行策略影响。

### 使用 PowerShell 和 Git

```powershell
git clone https://github.com/linni03/vibeai-image-skill.git
cd vibeai-image-skill
.\install.bat
```

Windows 原生环境会使用当前用户的 DPAPI 加密 API Key。配置保存在 `%CODEX_HOME%\sub2api-image\config.json`；未设置 `CODEX_HOME` 时使用 `%USERPROFILE%\.codex\sub2api-image\config.json`。配置文件中不会保存明文 Key，也不会使用无效的 Unix `0600` 权限声明。

从旧版本更新时，安装器会读取 `%USERPROFILE%\.config\sub2api-image\config.json` 并把配置写入新位置。旧文件会暂时保留，确认新版正常后可以手动删除。

## macOS、Linux 和 WSL2 安装

```bash
git clone https://github.com/linni03/vibeai-image-skill.git
cd vibeai-image-skill
sh install.sh
```

`install.sh` 会自动查找 `python3` 或 `python`。macOS、Linux 和 WSL2 使用权限为 `0600` 的配置文件保存 API Key。

## 安装器会做什么

安装器自动识别 Windows 原生、WSL、macOS 或 Linux，并显示实际使用的 Python、Codex Home、Skill 路径和配置路径。安装过程只会询问：

```text
Sub2API Base URL [https://vibeai.tech/v1]:
Sub2API 生图 API Key（输入隐藏）:
```

- 使用默认 Base URL：直接按回车
- 使用其他 Sub2API 地址：输入地址后按回车
- 输入 API Key 时终端不会显示字符，这是正常现象
- 已经配置过时，可以直接按回车保留现有 Key

Skill 会安装到 `$CODEX_HOME/skills/sub2api-image`；未设置 `CODEX_HOME` 时使用 `~/.codex/skills/sub2api-image`。Windows 配置保存在 `$CODEX_HOME/sub2api-image/config.json`，macOS、Linux 和 WSL2 配置保存在 `~/.config/sub2api-image/config.json`。

安装和配置不会生成图片、访问图像 API 或产生生图费用。安装器会暂存新版本，并在配置失败时恢复原来的 Skill，避免留下半安装状态。

## 重启并确认

安装完成后，退出并重新启动 Codex，或者新建一个 Codex 会话。然后发送：

```text
检查 sub2api-image 是否已经安装和配置，不要生成图片。
```

这项检查不会产生生图费用。

## Windows 一次性 Codex 权限设置

Skill 已经把普通生图缩减为一条正式命令，但 Codex 自己的沙箱仍然决定命令能否联网以及能写入哪些目录。希望以后直接生成到“图片”目录、不再逐次审批时，可以进行一次用户级配置。

先在 PowerShell 查看 Windows 实际的图片目录；这也适用于被 OneDrive 重定向的系统：

```powershell
[Environment]::GetFolderPath('MyPictures')
```

然后编辑 `%USERPROFILE%\.codex\config.toml`。把实际图片目录合并到已有配置，不要重复创建同名 TOML 表：

```toml
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true
writable_roots = ['C:\Users\YOUR-NAME\Pictures']
```

`network_access = true` 允许工作区命令联网；`writable_roots` 允许写入图片目录。只应在你接受该权限范围时启用。修改后重启 Codex 或新建会话，可用 `/status` 确认可写根。

如果仍在使用旧配置且暂时不重新安装，可以在 Windows 原生 Codex CLI 中先执行：

```text
/sandbox-add-read-dir C:\Users\YOUR-NAME\.config\sub2api-image
```

新配置迁移到 `%CODEX_HOME%` 后不再需要这条旧路径读取规则。

## 在 Codex 中使用

安装成功后直接描述需求，例如：

- 生成图片：`生成一张 2K 横向的赛博朋克城市图片，保存到当前项目目录。`
- 保存到 Windows 图片目录：`生成一张治愈的像素风景图片，2K 横向，保存到图片目录，文件名 healing_pixel_landscape_2k.png。`
- 编辑图片：`编辑 ./source.png，把背景换成雪山，保持人物不变。`
- 使用参考图：`使用 ./subject.png 作为主体，参考 ./style.webp 的色彩风格生成一张 1K 图片。`
- 指定格式：`生成一张透明背景的产品图，保存为 ./product.webp。`

如果 Codex 没有自动选择该 Skill，可以显式指定：

```text
使用 $sub2api-image 生成一张 1K 横向图片并保存到当前目录。
```

## 更新或重新配置

在仓库目录执行 `git pull`，然后重新运行对应平台的安装器：

Windows：

```powershell
git pull
.\install.bat
```

macOS、Linux 或 WSL2：

```bash
git pull
sh install.sh
```

## 常见问题

- **Windows 提示找不到 Python**：从 [Python 官网](https://www.python.org/downloads/windows/) 安装 Python 3.10 或更高版本，然后重新双击 `install.bat`。
- **Codex 找不到 Skill**：重启 Codex 或新建会话，并尝试显式写 `$sub2api-image`。
- **提示未配置或认证失败**：重新运行安装器，检查 Base URL、API Key，以及该 Key 所属用户组是否已启用图像生成功能。
- **Windows 权限审批服务返回 502**：在 `%USERPROFILE%\.codex\config.toml` 设置 `approvals_reviewer = "user"` 后重启 Codex，避免把本机审批交给自动审核；也可以对最近一次自动审核拒绝使用 `/approve` 重试一次。
- **图片目录仍要求写入审批**：用 `/status` 检查实际 Pictures 路径是否已经出现在 writable roots，并确认路径与 `[Environment]::GetFolderPath('MyPictures')` 的输出一致。
- **API Key 是否安全**：Windows 使用当前用户 DPAPI 加密；macOS、Linux 和 WSL2 使用 `0600` 配置文件。不要把 Key 粘贴到 Codex 对话、命令参数、URL、Git 仓库或日志里。
- **是否会产生费用**：安装和配置不会产生费用；只有实际生成或编辑图片时才会消耗 Sub2API 额度。
