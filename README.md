# MAS-004_RPI-Databridge

Reliable HTTPS send/receive service for the Raspberry Pi PLC setup with persistent inbox/outbox, watchdog handling and local web UI.

## Python fuer Schulung und Entwicklung

- Teamstandard fuer neue Entwicklungsrechner: `Python 3.13.x`
- `Python 3.12.x` ist als Fallback okay, wenn `3.13` auf dem Zielsystem nicht sauber verfuegbar ist
- `Python 3.14` derzeit nicht als Schulungsstandard verwenden
- `requires-python = ">=3.9"` im `pyproject.toml` beschreibt nur die technische Mindestversion, nicht die empfohlene Teamversion
- Der TEST-Raspi laeuft aktuell produktiv mit `Python 3.9.2`; deshalb wird die Runtime-Mindestversion im Projekt bewusst nicht angehoben

## Lokale Entwicklung unter Windows

```powershell
cd MAS-004_RPI-Databridge
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

## Deployment auf Raspberry Pi

Auf dem Zielsystem darf eine freigegebene Python-Version `>= 3.9` verwendet werden.
Solange der Raspberry auf Raspbian 11 / Python 3.9.2 basiert, bleibt dies die verbindliche Laufzeitbasis.

## Weitere Doku

- `docs/Microtom_Interface_QuickStart.md`
- `docs/SUPPORT_RUNBOOK.md`
- `docs/PROJECT_CONTEXT.md`
