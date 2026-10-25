<p align="center">
  <img src="assets/readme/hero.svg" width="100%" alt="MCP-Plag: ChatGPT connects to Mail.ru and Yandex Music through MCP">
</p>

<p align="center">
  <a href="README.md">Русский</a> · <strong>English</strong> · <a href="README.zh-CN.md">简体中文</a> · <a href="README.ja.md">日本語</a> · <a href="README.de.md">Deutsch</a>
</p>

# MCP-Plag

**Email and music in ChatGPT, with server source code you can host yourself.**

A collection of ChatGPT plugins and their MCP servers: ready-made packages, code, icons, setup guides, and checks. The repository contains two integrations: Mail.ru and Yandex Music.

## Choose an integration

| Integration | What you can do | Source and packages |
| --- | --- | --- |
| **Mail / Mail.ru** | Search and read email; work with drafts, attachments, and folders | [Overview](Mail.ru/README.md) · [Archives](Mail.ru/releases/) |
| **Yandex Music** | Find music, get recommendations, and manage your playlists | [Overview](Yandex.Music/README.md) · [Archives](Yandex.Music/releases/) |

### Mail: from a message to a reply draft

> “Find the meeting email, show the thread, and save a reply to everyone as a draft.”

**Mail 1.0.0 · MCP 0.4.0 · 31 tools.** Supports `mail.ru`, `inbox.ru`, `list.ru`, `bk.ru`, and `internet.ru` mailboxes. Read up to 20 messages per request, reply and forward with attachments, archive messages, change read status, and manage folders.

Replies and forwards create drafts by default. Sending requires an explicit request; permanent deletion and emptying the trash also require separate requests. [Mail features and limits →](Mail.ru/README.md)

### Yandex Music: from search to a playlist

> “Find tracks by this artist and add the ones I select to my playlist.”

**Plugin 1.0.3 · 14 music tools · server snapshot dated October 25, 2026.** Search the catalog, explore artist and album tracks, charts, liked tracks, listening history, My Wave, and similar tracks. Create private playlists, add up to 100 tracks per call, or delete your own playlists on explicit request.

This is a private installation using one server account. Audio streaming and downloads are not provided. [Yandex Music features and setup →](Yandex.Music/README.md)

## Host your own server

The connection is **ChatGPT → MCP server → selected service**. A plugin package links ChatGPT to a registered app; the server carries out email or music operations.

1. Open the integration overview and download its plugin package and server archive.
2. Deploy the server, install dependencies, and configure HTTPS and private settings using its guide.
3. Register your own app with your server’s HTTPS endpoint and replace the app binding in the plugin package.

The identifiers in the published packages belong to the author’s installation. Copying a ZIP does not create a separate integration. The current servers run on **VDSina**; you can deploy your own instance on another VPS/VDS or a machine with a reachable HTTPS address. Yandex Music also includes an SSH connection mode.

[Deploy Mail](Mail.ru/server/README.md) · [Deploy Yandex Music](Yandex.Music/server/README.md)

## Data and access

- **Configure secrets separately.** Production passwords, tokens, keys, databases, and logs are excluded from published packages.
- **Mail uses an app password.** Sign in through the HTTPS page; you do not need to send the password in chat. The server supports separate connections and encrypts stored passwords with Fernet.
- **Yandex Music uses the server’s account.** OAuth access is restricted to its owner; this configuration does not provide separate music accounts for different users.
- **Your server processes the data.** Requested results, including email bodies and attachments, reach ChatGPT through MCP. The server administrator controls its configuration, database, and keys.

See each integration’s guide for Mail attachment limits, authorization requirements, and checks. Mail is intended for a small private installation; test the scenarios you need with a test account before broader use.

## Repository contents

```text
Mail.ru/           Mail plugin, MCP server, tests, and ZIP files
Yandex.Music/      Yandex Music plugin, MCP server, and ZIP files
assets/readme/     Editable SVG connection diagram
README*.md        Descriptions in five languages
```

Rebuild the archives and verify their contents and checksums from the repository root:

```sh
python Mail.ru/tools/build_releases.py
python Mail.ru/tools/verify_package.py
python Yandex.Music/tools/build_releases.py
python Yandex.Music/tools/verify_package.py
```

Each integration directory contains testing instructions and version information. Building archives does not require accessing real mail or changing music playlists. Detailed integration guides are currently in Russian.
