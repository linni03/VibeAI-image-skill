# VibeAI Sub2API 图像技能

`sub2api-image` 是一个供 Codex 使用的图像生成与编辑技能。它通过用户自己的 Sub2API 密钥调用兼容 OpenAI 接口的图像 API，在本地生成或编辑 PNG、JPEG 和 WebP 文件。

该技能使用项目提供的同步图像接口，并会检查返回文件的实际内容和尺寸；它不依赖 Codex 内置的图像工具。

## 通过 Codex 安装

发布本仓库并创建 `v1.0.0` 标签后，可以向 Codex 发送以下请求：

```text
使用 $skill-installer 从
https://github.com/<owner>/vibeai-image-skill/tree/v1.0.0/skills/sub2api-image
安装 sub2api-image。
然后在交互式终端中启动技能的 configure.py，配置我的
Sub2API 接口，并运行 1K 冒烟测试。不要把我的 API 密钥放进 shell 命令，
也不要在回复中重复我的 API 密钥。
```

安装程序会将技能放置在以下路径之一：

```text
$CODEX_HOME/skills/sub2api-image
~/.codex/skills/sub2api-image
```

技能会在 Codex 的下一轮对话中生效。

默认接口地址为 `https://vibeai.tech/v1`，默认模型为 `gpt-image-2`。每位用户都需要拥有独立的 Sub2API 用户密钥，并且该密钥所属的用户组必须允许图像生成。

## 安全配置

最安全的配置方式是在本地终端直接运行配置脚本，这样密钥不会进入聊天记录：

```bash
python3 ~/.codex/skills/sub2api-image/scripts/configure.py
```

脚本会关闭密钥回显，并将配置以 JSON 格式保存到：

```text
~/.config/sub2api-image/config.json
```

配置文件权限会设置为 `0600`，只有当前用户可以读取。

不要把 API 密钥粘贴到 Codex 对话中。已经出现在公开或共享聊天记录中的密钥，建议立即撤销并重新生成。

查看非敏感配置，或删除本地配置：

```bash
python3 ~/.codex/skills/sub2api-image/scripts/configure.py --show
python3 ~/.codex/skills/sub2api-image/scripts/configure.py --revoke
```

## 使用方式

Codex 重新加载技能列表后，可以直接调用 `$sub2api-image`，也可以用自然语言描述需求，例如：

```text
生成一张 2K 横向赛博朋克城市图片，并保存到当前项目目录。
```

```text
编辑 /path/to/source.png，将背景改成水彩纸效果。
```

如果没有指定尺寸，技能默认生成 1K 正方形图片。2K 和 4K 是否可用取决于当前渠道。客户端会检查返回图片的实际像素尺寸，不会把低于请求规格的图片标记为对应的尺寸等级。

## 环境要求

- Python 3.10 或更高版本
- 可访问的 Sub2API 部署，并提供以下接口：
  - `/v1/images/generations`
  - `/v1/images/edits`
- 一个已启用图像生成功能的 Sub2API 用户密钥

本项目不需要安装第三方 Python 包。

## 测试与计费说明

客户端提供冒烟测试，但无法读取仅管理员可访问的计费日志。完成测试后，Sub2API 管理员需要在管理后台确认以下信息：

- 图像生成数量
- 图像尺寸来源
- 各尺寸的生成数量
- 实际扣费
