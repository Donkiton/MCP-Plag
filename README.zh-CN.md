<p align="center">
  <img src="assets/readme/hero.svg" width="100%" alt="MCP-Plag：ChatGPT 通过 MCP 连接 Mail.ru 和 Yandex Music">
</p>

<p align="center">
  <a href="README.md">Русский</a> · <a href="README.en.md">English</a> · <strong>简体中文</strong> · <a href="README.ja.md">日本語</a> · <a href="README.de.md">Deutsch</a>
</p>

# MCP-Plag

**在 ChatGPT 中使用邮件和音乐，并获取可自行部署的服务器源码。**

本仓库收集 ChatGPT 插件及其 MCP 服务器，提供安装包、代码、图标、部署说明和检查工具。目前包含 Mail.ru 和 Yandex Music 两项集成。

## 选择集成

| 集成 | 功能 | 源码与安装包 |
| --- | --- | --- |
| **Mail / Mail.ru** | 搜索和阅读邮件，管理草稿、附件和文件夹 | [概览](Mail.ru/README.md) · [压缩包](Mail.ru/releases/) |
| **Yandex Music** | 搜索音乐、获取推荐、管理自己的歌单 | [概览](Yandex.Music/README.md) · [压缩包](Yandex.Music/releases/) |

### Mail：从邮件到回复草稿

> “找到有关会议的邮件，显示往来邮件，并将回复所有人的内容保存为草稿。”

**Mail 1.0.0 · MCP 0.4.0 · 31 个工具。** 支持 `mail.ru`、`inbox.ru`、`list.ru`、`bk.ru` 和 `internet.ru` 邮箱。每次可读取最多 20 封邮件，支持带附件回复和转发、归档、修改已读状态以及管理文件夹。

回复和转发默认保存为草稿。发送邮件需要用户明确提出；永久删除和清空垃圾箱也需要单独明确提出。[Mail 功能与限制 →](Mail.ru/README.md)

### Yandex Music：从搜索到歌单

> “查找这位歌手的歌曲，并把我选中的歌曲添加到我的歌单。”

**插件 1.0.3 · 14 个音乐工具 · 服务器快照日期：2026 年 10 月 25 日。** 支持目录搜索、歌手和专辑曲目、排行榜、喜欢的歌曲、收听历史、My Wave 个性化推荐及相似歌曲。可创建私人歌单，每次添加最多 100 首歌曲，并按明确要求删除自己的歌单。

这是使用单一服务器账号的私人部署，不提供音频流播放或下载。[Yandex Music 功能与配置 →](Yandex.Music/README.md)

## 部署自己的服务器

连接流程为 **ChatGPT → MCP 服务器 → 所选服务**。插件包将 ChatGPT 与已注册的应用关联，服务器负责执行邮件或音乐操作。

1. 打开所需集成的概览，下载插件包和服务器压缩包。
2. 按照该集成的说明部署服务器、安装依赖，并配置 HTTPS 和私密设置。
3. 注册自己的应用，填写服务器的 HTTPS 端点，并替换插件包中的应用关联信息。

已发布包中的标识符属于作者的部署实例。复制 ZIP 文件本身不会创建独立集成。当前服务器运行在 **VDSina**；自行部署时，也可以使用其他 VPS/VDS 或具有可访问 HTTPS 地址的机器。Yandex Music 还保留了 SSH 连接模式。

[部署 Mail](Mail.ru/server/README.md) · [部署 Yandex Music](Yandex.Music/server/README.md)

## 数据与访问范围

- **密钥单独配置。** 发布的包不包含生产环境密码、令牌、密钥、数据库或日志。
- **Mail 使用应用专用密码。** 通过 HTTPS 页面登录，无需在聊天中发送密码。服务器支持独立连接，并使用 Fernet 加密存储密码。
- **Yandex Music 使用服务器账号。** OAuth 访问仅限该账号所有者；此配置不支持为不同用户提供独立音乐账号。
- **服务器会处理相关数据。** 用户请求的结果，包括邮件正文和附件，通过 MCP 传给 ChatGPT。所选服务器的管理员控制配置、数据库和密钥。

Mail 附件限制、授权要求和验证方式详见各集成的说明。Mail 适用于小规模私人部署；扩大使用范围前，请用测试账号验证所需场景。

## 仓库内容

```text
Mail.ru/           Mail 插件、MCP 服务器、测试和 ZIP 包
Yandex.Music/      Yandex Music 插件、MCP 服务器和 ZIP 包
assets/readme/     可编辑的 SVG 连接图
README*.md        五种语言的项目说明
```

在仓库根目录重新构建压缩包，并验证内容与校验和：

```sh
python Mail.ru/tools/build_releases.py
python Mail.ru/tools/verify_package.py
python Yandex.Music/tools/build_releases.py
python Yandex.Music/tools/verify_package.py
```

各集成目录中提供测试说明和版本信息。构建压缩包无需访问真实邮件或修改音乐歌单。详细集成说明目前为俄语。
