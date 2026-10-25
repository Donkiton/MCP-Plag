<p align="center">
  <img src="assets/readme/hero.svg" width="100%" alt="MCP-Plag: ChatGPT verbindet sich über MCP mit Mail.ru und Yandex Music">
</p>

<p align="center">
  <a href="README.md">Русский</a> · <a href="README.en.md">English</a> · <a href="README.zh-CN.md">简体中文</a> · <a href="README.ja.md">日本語</a> · <strong>Deutsch</strong>
</p>

# MCP-Plag

**E-Mail und Musik in ChatGPT – mit Servercode für den eigenen Betrieb.**

Eine Sammlung von ChatGPT-Plugins und ihren MCP-Servern: fertige Pakete, Quellcode, Symbole, Anleitungen und Prüfwerkzeuge. Das Repository enthält zwei Integrationen: Mail.ru und Yandex Music.

## Integration auswählen

| Integration | Funktionen | Quellcode und Pakete |
| --- | --- | --- |
| **Mail / Mail.ru** | E-Mails suchen und lesen; Entwürfe, Anhänge und Ordner verwalten | [Übersicht](Mail.ru/README.md) · [Archive](Mail.ru/releases/) |
| **Yandex Music** | Musik suchen, Empfehlungen abrufen und eigene Playlists verwalten | [Übersicht](Yandex.Music/README.md) · [Archive](Yandex.Music/releases/) |

### Mail: von der Nachricht zum Antwortentwurf

> „Finde die E-Mail zum Treffen, zeige den Verlauf und speichere eine Antwort an alle als Entwurf.“

**Mail 1.0.0 · MCP 0.4.0 · 31 Werkzeuge.** Unterstützt Postfächer bei `mail.ru`, `inbox.ru`, `list.ru`, `bk.ru` und `internet.ru`. Bis zu 20 Nachrichten pro Anfrage lesen, mit Anhängen antworten oder weiterleiten, Nachrichten archivieren, den Lesestatus ändern und Ordner verwalten.

Antworten und Weiterleitungen werden standardmäßig als Entwurf gespeichert. Der Versand erfordert einen ausdrücklichen Auftrag; das endgültige Löschen und das Leeren des Papierkorbs ebenfalls jeweils einen gesonderten Auftrag. [Funktionen und Grenzen von Mail →](Mail.ru/README.md)

### Yandex Music: von der Suche zur Playlist

> „Suche Titel dieses Künstlers und füge meine Auswahl zu meiner Playlist hinzu.“

**Plugin 1.0.3 · 14 Musikwerkzeuge · Serverstand vom 25. Oktober 2026.** Katalogsuche, Titel von Künstlern und Alben, Charts, Lieblingssongs, Hörverlauf, My Wave und ähnliche Titel. Private Playlists erstellen, bis zu 100 Titel pro Aufruf hinzufügen und eigene Playlists auf ausdrücklichen Auftrag löschen.

Diese private Installation verwendet ein einzelnes Serverkonto. Audiostreaming und Downloads werden nicht angeboten. [Funktionen und Einrichtung von Yandex Music →](Yandex.Music/README.md)

## Auf dem eigenen Server betreiben

Die Verbindung folgt dem Weg **ChatGPT → MCP-Server → ausgewählter Dienst**. Das Plugin-Paket verknüpft ChatGPT mit einer registrierten App; der Server führt die E-Mail- oder Musikaktionen aus.

1. Die Übersicht der gewünschten Integration öffnen und das Plugin-Paket sowie das Serverarchiv herunterladen.
2. Den Server nach seiner Anleitung einrichten, Abhängigkeiten installieren sowie HTTPS und private Einstellungen konfigurieren.
3. Eine eigene App mit dem HTTPS-Endpunkt des Servers registrieren und die App-Zuordnung im Plugin-Paket ersetzen.

Die Kennungen in den veröffentlichten Paketen gehören zur Installation des Autors. Das Kopieren einer ZIP-Datei erstellt noch keine eigenständige Integration. Die aktuellen Server laufen bei **VDSina**. Eine eigene Instanz kann auch auf einem anderen VPS/VDS oder einem Rechner mit erreichbarer HTTPS-Adresse betrieben werden. Yandex Music enthält außerdem einen SSH-Verbindungsmodus.

[Mail bereitstellen](Mail.ru/server/README.md) · [Yandex Music bereitstellen](Yandex.Music/server/README.md)

## Daten und Zugriffsgrenzen

- **Zugangsdaten separat konfigurieren.** Produktive Passwörter, Tokens, Schlüssel, Datenbanken und Protokolle sind in den veröffentlichten Paketen nicht enthalten.
- **Mail nutzt ein App-Passwort.** Die Anmeldung erfolgt über eine HTTPS-Seite; das Passwort muss nicht im Chat übermittelt werden. Der Server unterstützt getrennte Verbindungen und verschlüsselt gespeicherte Passwörter mit Fernet.
- **Yandex Music nutzt das Serverkonto.** Der OAuth-Zugriff ist auf dessen Eigentümer beschränkt. Die Konfiguration stellt keine getrennten Musikkonten für verschiedene Nutzer bereit.
- **Der Server verarbeitet die Daten.** Angeforderte Ergebnisse, einschließlich E-Mail-Inhalten und Anhängen, werden über MCP an ChatGPT übermittelt. Der Serveradministrator verwaltet Konfiguration, Datenbank und Schlüssel.

Größenlimits für Mail-Anhänge, Autorisierungsbedingungen und Prüfverfahren stehen in den jeweiligen Anleitungen. Mail ist für eine kleine private Installation vorgesehen. Die benötigten Abläufe vor einem breiteren Einsatz mit einem Testkonto prüfen.

## Inhalt des Repositorys

```text
Mail.ru/           Mail-Plugin, MCP-Server, Tests und ZIP-Dateien
Yandex.Music/      Yandex-Music-Plugin, MCP-Server und ZIP-Dateien
assets/readme/     Editierbare SVG-Verbindungsgrafik
README*.md        Projektbeschreibungen in fünf Sprachen
```

Archive aus dem Stammverzeichnis neu erstellen und Inhalte sowie Prüfsummen prüfen:

```sh
python Mail.ru/tools/build_releases.py
python Mail.ru/tools/verify_package.py
python Yandex.Music/tools/build_releases.py
python Yandex.Music/tools/verify_package.py
```

Die Integrationsverzeichnisse enthalten Testanleitungen und Versionsangaben. Das Erstellen der Archive benötigt weder Zugriff auf echte E-Mails noch Änderungen an Musikplaylists. Die ausführlichen Integrationsanleitungen sind derzeit auf Russisch verfügbar.
