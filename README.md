# lg-hue-sync-deployer

Unraid-Container, der [adeze/lg-hue-sync](https://github.com/adeze/lg-hue-sync) (Philips-Hue-Entertainment-Ambilight direkt auf gerooteten LG webOS TVs) baut und per Web-Oberfläche auf den TV schickt.

- Baut mit derselben Toolchain wie upstream `docker/Dockerfile.cross`: Debian Buster, `armv7-unknown-linux-gnueabi`, soft-float, glibc ≤ 2.28 (Ceiling LG C1 / webOS 6).
- Prüft jedes Artefakt vor dem Deploy: ELF32 ARM EABI5, soft-float, höchstes `GLIBC_` ≤ 2.28.
- Erkennt neue Upstream-Commits (Intervall einstellbar) und zeigt sie an.
- Deployt mit den Upstream-Skripten:
  - **Erstinstallation**: `scripts/deploy.sh <tv-ip> <port> --reuse` (Binary, Luna-Rechte, systemd-Dienst, Homebrew-Channel-Autostart, Log-Rotation). Die Launcher-Kachel wird ausgelassen (braucht `uv` + `ares-rs`), das Dashboard läuft unter `http://<tv-ip>:8088/`.
  - **Update**: `make deploy-bin` (behält `config.json` und Pairing, sichert Vorversion als `.previous`, prüft SHA-256).
  - **Rollback**: Befehl aus `docs/operations.md`.
- Zeigt TV-Status: Dienst, installierter Binary-Hash vs. Build, `GET /api/status` vom TV.

## Voraussetzungen

- TV gerootet, Root-SSH aktiv (Homebrew Channel → Settings → SSH).
- Unraid muss den TV per SSH (Port 22) und HTTP (8088) erreichen.
- Erster Build: einige Minuten, lädt Crates von crates.io; Cache liegt unter `/config/cargo` und `/config/target-cache` (rechne mit 1–3 GB).

## Einrichtung

1. Container mit Template `unraid/lg-hue-sync-deployer.xml` anlegen, `TV_IP` setzen.
2. WebUI öffnen → SSH-Schlüssel des Containers auf dem TV hinterlegen (Passwort-Button oder manuell in `~/.ssh/authorized_keys`).
3. **SSH testen** → **Holen & bauen** → **Erstinstallation**.
4. Auf `http://<tv-ip>:8088/` Hue Bridge pairen und Entertainment-Bereich wählen.
5. Später: **Nach Updates suchen** → **Bauen & senden**.

## Variablen

| Variable | Default | Zweck |
|---|---|---|
| `TV_IP` | – | IP des TV |
| `TV_SSH_PORT` | `22` | Root-SSH-Port |
| `TV_DASHBOARD_PORT` | `8088` | lg-hue-sync-Dashboard auf dem TV |
| `CHECK_INTERVAL_HOURS` | `6` | Upstream-Prüfung, `0` = aus |
| `AUTO_BUILD` | `false` | neue Version automatisch bauen |
| `AUTO_DEPLOY` | `false` | nach Auto-Build direkt senden |
| `UI_TOKEN` | leer | wenn gesetzt: WebUI mit `?k=<token>` |
| `UPSTREAM_REPO` / `UPSTREAM_BRANCH` | adeze/lg-hue-sync / `main` | Quelle |
| `SSH_KEY_TYPE` | `ed25519` | `rsa`, falls der TV ed25519 ablehnt |

## Sicherheit

Die WebUI kann Root-Binaries auf den TV schreiben. Nur im LAN betreiben, nicht nach außen freigeben, bei Bedarf `UI_TOKEN` setzen. POST-Aufrufe verlangen einen Custom-Header, fremde Webseiten können also keine Jobs auslösen. Der private SSH-Schlüssel liegt in `/config/.ssh`.

## Lizenz

MIT. lg-hue-sync selbst: MIT, © adeze.
