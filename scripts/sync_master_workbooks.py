from __future__ import annotations

import argparse
import shutil
from copy import copy
from pathlib import Path
from typing import Any

from openpyxl import load_workbook


ROOT_GIT_REPO = Path(__file__).resolve().parents[2]
RPI_REPO = Path(__file__).resolve().parents[1]
DEFAULT_PARAM_SOURCE = ROOT_GIT_REPO / "Parameterliste SAR41-MAS-004.xlsx"
DEFAULT_PARAM_REPO_COPY = RPI_REPO / "master_data" / "Parameterliste SAR41-MAS-004.xlsx"
DEFAULT_IO_SOURCE = Path(
    r"d:\Users\Egli_Erwin\Veralto\DE-SMD-Support-Switzerland - Documents\11_SAR\01_CH"
    r"\SAR41-MAS-004_Roche_TTO_LSR_Label und Kontrollsystem_V2\03_Dokumentation\SPS I_Os"
    r"\SAR41-MAS-004_SPS_I-Os.xlsx"
)
DEFAULT_IO_REPO_COPY = RPI_REPO / "master_data" / "SAR41-MAS-004_SPS_I-Os.xlsx"


SPECIAL_AI: dict[str, str] = {
    "MAP0014": "KI: Ich verstehe diesen Parameter als Soll-Vorzugsgeschwindigkeit des Etikettenbandes im normalen Prozessbetrieb. Der ESP muss diesen Wert fuer die Taktbewegungen und fuer die dynamische Nachregelung des Etikettenantriebs verwenden; auf dem Raspi ist er vor allem fuer Transparenz, Logging und Bedienung relevant.",
    "MAP0015": "KI: Ich verstehe diesen Parameter als Rueckspulgeschwindigkeit fuer den Rueckspulbetrieb und fuer Ruecksetzbewegungen nach Fehlern bzw. Entnahmekontrolle. Dieser Wert ist prozesskritisch fuer den ESP, weil die Rueckspulung damit weich und reproduzierbar laufen soll.",
    "MAP0016": "KI: Ich verstehe diesen Parameter als Auswahl zwischen TTO und Laser. Es darf immer nur eines der beiden Drucksysteme fuehrend aktiv sein; davon haengen Triggerweg, Bypasslogik und die nachfolgenden Bewertungsbits im Label-Schieberegister ab.",
    "MAP0017": "KI: Ich verstehe diesen Parameter als Distanz vom Einlese-Sensor bis zur Material-Kontrollkamera. Diese Distanz ist ein fester Wegbezug im Label-Schieberegister, damit die Kamera fuer jedes bereits eingelesene Label an der richtigen Stelle getriggert wird.",
    "MAP0018": "KI: Ich verstehe diesen Parameter als Distanz vom Einlese-Sensor bis zur Druckposition des Lasers. Nur wenn MAP0016 auf Laser steht und kein Bypass aktiv ist, darf der ESP an genau dieser Wegposition den Laser-Trigger ausloesen.",
    "MAP0019": "KI: Ich verstehe diesen Parameter als Distanz vom Einlese-Sensor bis zur Druckposition des TTO. Nur wenn MAP0016 auf TTO steht und kein Bypass aktiv ist, darf der ESP an genau dieser Wegposition den TTO-Trigger ausloesen.",
    "MAP0020": "KI: Ich verstehe diesen Parameter als Distanz vom Einlese-Sensor bis zur OCR-/Verifizierungs-Kamera. Dieser Wegwert wird im Label-Schieberegister fuer den spaeteren OCR-Trigger und die Rueckmeldung von Verifizierung OK gebraucht.",
    "MAP0021": "KI: Ich verstehe diesen Parameter als Distanz vom Einlese-Sensor bis zum Kontrollsensor am Tischende. Dort wird geprueft, ob ein gutes Label vorhanden bleiben durfte bzw. ein schlechtes Label entnommen wurde.",
    "MAP0022": "KI: Ich verstehe diesen Parameter als Rueckspuldistanz, wenn ein Label am Kontrollsensor haette entnommen sein muessen, aber noch erkannt wurde. Der ESP muss dann das Band kontrolliert zuruecksetzen, ohne bereits bearbeitete Labels im Schieberegister fachlich noch einmal neu zu behandeln.",
    "MAP0023": "KI: Ich verstehe diesen Parameter als Vorwarnschwelle fuer den Abwickler in Prozent. Wenn MAS008 unter diesen Wert faellt, soll daraus die passende Warnung fuer Materialende entstehen.",
    "MAP0024": "KI: Ich verstehe diesen Parameter als Vorwarnschwelle fuer den Aufwickler in Prozent. Wenn MAS009 unter diesen Wert faellt, soll daraus die passende Warnung fuer Materialende bzw. Rollenfuellstand entstehen.",
    "MAP0025": "KI: Ich verstehe diesen Parameter als harte Produktions-Pausenschwelle fuer den Abwickler. Wird der verbleibende Fuellstand unterschritten, muss die Anlage in den Pausenzustand wechseln, damit kein unkontrollierter Produktionsabbruch entsteht.",
    "MAP0035": "KI: Ich verstehe diesen Parameter als Bypass fuer das aktive Drucksystem. Wenn er gesetzt ist, darf das jeweilige Drucksystem den Prozess nicht blockieren; die Druckbewertung im Labelregister muss dann fachlich als gebypasst behandelt werden.",
    "MAP0036": "KI: Ich verstehe diesen Parameter als Bypass der Material-Kontrollkamera. Wenn der Bypass aktiv ist, darf fehlendes Kamerafeedback nicht zu einem schlechten Material-OK-Bit fuehren.",
    "MAP0037": "KI: Ich verstehe diesen Parameter als Bypass der Verifizierungs-/OCR-Kamera. Wenn der Bypass aktiv ist, darf fehlendes OCR-Feedback das Label nicht als schlecht markieren.",
    "MAP0038": "KI: Ich verstehe diesen Parameter als Bypass fuer den Etiketten-Kontrollsensor am Auslauf. Wenn der Bypass aktiv ist, darf die Anlage fehlende Entnahmekontrolle nicht als Produktionsfehler werten.",
    "MAP0047": "KI: Ich verstehe diesen Parameter als Vorgabe, ob innen- oder aussengewickeltes Material verarbeitet wird. Daraus ergeben sich die Drehrichtungen fuer Auf- und Abwickler; der ESP soll diesen Wert lesen und fuer die Wicklerlogik umsetzen.",
    "MAP0056": "KI: Ich verstehe diesen Parameter als Sollposition der Portalachse X fuer Format bzw. Maschinenbewegung. Die eigentliche Anfahrt und Rueckmeldung erfolgt ueber den Oriental-Antrieb mit Motor-ID 1.",
    "MAP0057": "KI: Ich verstehe diesen Parameter als Sollposition der Portalachse Z fuer Format bzw. Maschinenbewegung. Die eigentliche Anfahrt und Rueckmeldung erfolgt ueber den Oriental-Antrieb mit Motor-ID 2.",
    "MAP0058": "KI: Ich verstehe diesen Parameter als Sollbewegung bzw. Sollposition des Etikettenantriebs in 1/10 mm. Dieser Parameter ist im Produktionsprozess besonders sensibel, weil daraus die genaue Bandposition fuer Druck und Kameratrigger entsteht.",
    "MAP0059": "KI: Ich verstehe diesen Parameter als Sollposition des Laser-Schutzblechs ueber Motor-ID 4. Solange der Laser fachlich noch nicht im Fokus steht, bleibt der Zusammenhang teilweise offen; die Achse muss aber trotzdem sauber parametrierbar bleiben.",
    "MAP0060": "KI: Ich verstehe diesen Parameter als Sollposition der Material-Kontrollkamera quer zum Band ueber Motor-ID 5. Dieser Wert ist formatabhaengig und bestimmt, wo die Kamera das Material liest.",
    "MAP0061": "KI: Ich verstehe diesen Parameter als Sollposition des Einlese-Sensors quer zum Band ueber Motor-ID 6. Der Sensor muss so positioniert werden, dass das Label sauber eingemessen wird.",
    "MAP0062": "KI: Ich verstehe diesen Parameter als Sollposition des Kontrollsensors am Auslauf quer zum Band ueber Motor-ID 7. Dieser Wert bestimmt, wo die Label-Entnahme bzw. Restanwesenheit geprueft wird.",
    "MAP0063": "KI: Ich verstehe diesen Parameter als Sollposition des linken bzw. Auslauf-Etikettenanschlags ueber Motor-ID 8. Dieser Wert folgt normalerweise der eingestellten Materialbreite.",
    "MAP0064": "KI: Ich verstehe diesen Parameter als Sollposition des rechten bzw. Einlauf-Etikettenanschlags ueber Motor-ID 9. Dieser Wert folgt normalerweise der eingestellten Materialbreite und sollte konsistent zu MAP0063 sein.",
    "MAP0065": "KI: Ich verstehe diesen Parameter als Bitmaske fuer die Freigabe der Maschinen-Tasten je nach Microtom-Benutzerlevel. Der Raspi nutzt diese Bitmaske, um Tastereingaben zu erlauben oder zu sperren und um die zugehoerigen Taster-LEDs nur dann zu signalisieren, wenn die Bedienhandlung fachlich und rechtebezogen erlaubt ist.",
    "MAP0066": "KI: Ich verstehe diesen Parameter als Distanz vom Einlese-Sensor bis zur ersten LED des 1-m-LED-Streifens. Der ESP braucht diesen Wegbezug, um den Labelstatus entlang des Streifens korrekt anzuzeigen; der exakte Default muss spaeter an der realen Maschine verifiziert werden.",
    "MAS0001": "KI: Ich verstehe diesen Parameter als fuehrenden Anlagenstatus, den der Raspi gegenueber Microtom meldet. Die eigentliche Statuslogik entsteht aus Bedienbefehlen, Sicherheitsbedingungen und Maschinenfortschritt; der ESP liefert dafuer harte Prozessereignisse zu.",
    "MAS0002": "KI: Ich verstehe diesen Parameter nicht als Status, sondern als Befehlsbyte von Microtom an die Anlage. Der Raspi muss diesen Befehlswert in den passenden internen Zielzustand uebersetzen, zum Beispiel Start, Stop, Einrichten, Synchronisieren, Leerfahren, Rueckspulen oder Pause.",
    "MAS0003": "KI: Ich verstehe diesen Parameter als verdichtete Produktionsrueckmeldung pro fertig behandeltem Label. Der ESP baut die Einzelinformationen im Schieberegister auf; der Raspi packt sie in das definierte Bitfeld und meldet den Wert an Microtom sowie in das Label-Produktionslog.",
    "MAS0008": "KI: Ich verstehe diesen Parameter als Fuellstand des Abwicklers in Prozent. Microtom liest ihn nur; der Wert wird aus der Wicklerlogik abgeleitet.",
    "MAS0009": "KI: Ich verstehe diesen Parameter als Fuellstand des Aufwicklers in Prozent. Microtom liest ihn nur; der Wert wird aus der Wicklerlogik abgeleitet.",
    "MAS0026": "KI: Ich verstehe diesen Parameter als Status des Abwicklers. Der Wert wird von der Wicklersteuerung fuehrend geliefert und vom ESP/Raspi fuer die Gesamtanlage weiterverwendet.",
    "MAS0027": "KI: Ich verstehe diesen Parameter als Status des Aufwicklers. Der Wert wird von der Wicklersteuerung fuehrend geliefert und vom ESP/Raspi fuer die Gesamtanlage weiterverwendet.",
    "MAS0028": "KI: Ich verstehe diesen Parameter als Purge- bzw. Abbruchereignis, das eine saubere Fortsetzung der laufenden Produktion verhindert. Solche Ereignisse muessen in den Not-Stop-/Stoerungszustand fuehren und sind fachlich staerker als eine normale Pause.",
    "MAS0029": "KI: Ich verstehe diesen Parameter als aktuelle Produktions- bzw. Auftrags-ID fuer Logfiles und Rueckverfolgbarkeit. Gemass Masterliste ist er fuer den ESP nicht relevant; fuehrend verwaltet wird er auf Raspi-/Microtom-Seite.",
    "MAS0030": "KI: Ich verstehe diesen Parameter als Status, ob Produktionslogfiles der letzten Produktion noch abholbereit sind. Auch dieser Wert ist gemaess Masterliste Raspi-/Microtom-seitig und soll nicht in den ESP gespiegelt werden.",
    "TTS0001": "KI: Ich verstehe diesen Parameter als numerischen Gesamtstatus des TTO-Druckers gemaess ZBC-Statusabbild. Der Wert muss sowohl aktiv aus Druckerstatuswechseln zurueckgemeldet als auch - soweit vom Drucker unterstuetzt - vom ESP gesetzt werden koennen, zum Beispiel fuer Offline, Online oder Shutdown.",
}


def row_key(ws, row_idx: int) -> str:
    ptype = str(ws.cell(row_idx, 1).value or "").strip().upper()
    pid = str(ws.cell(row_idx, 2).value or "").strip()
    return f"{ptype}{pid}" if ptype and pid else ""


def normalize_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", "\n").replace("\n", " ").split()).strip()


def copy_row_style(ws, src_row: int, dst_row: int, max_col: int):
    for col in range(1, max_col + 1):
        src = ws.cell(src_row, col)
        dst = ws.cell(dst_row, col)
        if src.has_style:
            dst._style = copy(src._style)
        if src.font:
            dst.font = copy(src.font)
        if src.fill:
            dst.fill = copy(src.fill)
        if src.border:
            dst.border = copy(src.border)
        if src.alignment:
            dst.alignment = copy(src.alignment)
        if src.protection:
            dst.protection = copy(src.protection)
        if src.number_format:
            dst.number_format = src.number_format


def access_text(rw: str, esp_rw: str) -> str:
    microtom_text = {
        "R": "Microtom soll diesen Wert nur lesen.",
        "W": "Microtom soll diesen Wert fuehrend schreiben koennen.",
        "R/W": "Microtom soll diesen Wert lesen und bei Bedarf auch schreiben koennen.",
        "N": "Dieser Wert ist fuer Microtom aktuell nicht freigegeben.",
    }.get(rw, f"Microtom-Rechte laut Tabelle: {rw or 'unbekannt'}.")
    esp_text = {
        "R": "Der ESP soll ihn nur lesen bzw. intern verwenden.",
        "W": "Der ESP darf ihn im Prozess aktiv verwenden und - falls fachlich vorgesehen - auch setzen oder rueckmelden.",
        "R/W": "Der ESP darf ihn lesen und schreiben.",
        "N": "Der ESP soll ihn gemaess Masterliste nicht verwenden.",
    }.get(esp_rw, f"ESP-Rechte laut Tabelle: {esp_rw or 'unbekannt'}.")
    return f"{microtom_text} {esp_text}"


def generic_ai_for_row(values: dict[str, str]) -> str:
    pkey = values["pkey"]
    ptype = values["ptype"]
    name = values["name"]
    message = values["message"]
    rw = values["rw"]
    esp_rw = values["esp_rw"]
    unit = values["unit"]
    fmt = values["format_relevant"]
    mapping = values["mapping"]
    existing = values["existing_ai"]

    if pkey in SPECIAL_AI:
        base = SPECIAL_AI[pkey]
    elif ptype == "MAP":
        desc = name or message or "einen Maschinen- bzw. Formatparameter"
        base = f"KI: Ich verstehe {pkey} als {desc}."
    elif ptype == "MAS":
        desc = name or message or "einen Maschinenstatus bzw. Maschinenwert"
        base = f"KI: Ich verstehe {pkey} als {desc}."
    elif ptype == "MAE":
        desc = name or message or "eine Stoerungsrueckmeldung"
        base = f"KI: Ich verstehe {pkey} als Stoerungsbit bzw. Stoerungsmeldung fuer {desc}."
    elif ptype == "MAW":
        desc = name or message or "eine Warnungsrueckmeldung"
        base = f"KI: Ich verstehe {pkey} als Warnungsbit bzw. Vorwarnung fuer {desc}."
    elif ptype == "TTP":
        desc = name or message or "einen TTO-Parameter"
        base = f"KI: Ich verstehe {pkey} als TTO-Betriebsparameter ({desc})."
    elif ptype in {"TTE", "TTW"}:
        desc = name or message or "eine TTO-Ereignis- bzw. Statusmeldung"
        base = f"KI: Ich verstehe {pkey} als vom TTO kommendes Ereignis bzw. Statussignal ({desc})."
    elif ptype == "TTS":
        desc = name or message or "einen TTO-Gesamtstatus"
        base = f"KI: Ich verstehe {pkey} als verdichteten TTO-Gesamtstatus ({desc})."
    elif ptype in {"LSE", "LSW"}:
        desc = name or message or "eine Laser-Ereignis- bzw. Statusmeldung"
        base = f"KI: Ich verstehe {pkey} als vom Laser kommendes Ereignis bzw. Statussignal ({desc})."
    else:
        desc = name or message or "einen derzeit noch nicht genauer beschriebenen Parameter"
        base = f"KI: Ich verstehe {pkey} als {desc}."

    extras: list[str] = []
    base_plain = base.removeprefix("KI:").strip()
    extras.append(access_text(rw, esp_rw))
    if unit:
        extras.append(f"Einheit bzw. Wertebereich laut Tabelle: {unit}.")
    if fmt:
        extras.append(
            "Der Wert ist formatrelevant und muss beim Formatwechsel konsistent mitgezogen werden."
            if fmt.upper() == "YES"
            else "Der Wert ist laut Tabelle nicht formatrelevant und eher als Maschinen-/Systemeinstellung zu sehen."
        )
    if mapping:
        extras.append(f"Technische Zuordnung laut Tabelle: {mapping}.")
    if existing and existing != base_plain and not existing.startswith("Ich verstehe "):
        extras.append(f"Vorhandene Projekt-Notiz: {existing}.")
    if not name and not message:
        extras.append("Die fachliche Bedeutung ist in der Tabelle aktuell noch nicht ausgeschrieben; das muss im Feldtest bzw. ueber Herstellerspezifikation weiter verifiziert werden.")

    return " ".join(part.strip() for part in [base, *extras] if part and part.strip())


def ensure_map0066(ws):
    existing_row = None
    map0065_row = None
    for row_idx in range(2, ws.max_row + 1):
        key = row_key(ws, row_idx)
        if key == "MAP0065":
            map0065_row = row_idx
        if key == "MAP0066":
            existing_row = row_idx
            break

    if existing_row is None:
        if map0065_row is None:
            raise RuntimeError("MAP0065 not found - cannot insert MAP0066 cleanly")
        target_row = map0065_row + 1
        if row_key(ws, target_row):
            ws.insert_rows(target_row, 1)
        copy_row_style(ws, map0065_row, target_row, ws.max_column)
        existing_row = target_row

    values = [
        "MAP",
        "0066",
        "0",
        "30000",
        "0",
        "1/10mm",
        "W",
        "W",
        "uint16",
        "MAP0066 Distanz Etikettenerfassung - erste LED LED-Streifen",
        None,
        "NO",
        "Distanz von der Etikettenerfassung bis zur ersten LED des LED-Streifens",
        None,
        None,
        None,
        SPECIAL_AI["MAP0066"],
    ]
    for col_idx, value in enumerate(values, start=1):
        ws.cell(existing_row, col_idx).value = value


def sync_parameter_workbook(path: Path):
    wb = load_workbook(path)
    ws = wb["Parameter"]
    ensure_map0066(ws)

    updated = 0
    for row_idx in range(2, ws.max_row + 1):
        ptype = str(ws.cell(row_idx, 1).value or "").strip().upper()
        pid = str(ws.cell(row_idx, 2).value or "").strip()
        if not ptype or not pid:
            continue
        values = {
            "pkey": f"{ptype}{pid}",
            "ptype": ptype,
            "name": normalize_text(ws.cell(row_idx, 10).value),
            "mapping": normalize_text(ws.cell(row_idx, 11).value),
            "format_relevant": normalize_text(ws.cell(row_idx, 12).value),
            "message": normalize_text(ws.cell(row_idx, 13).value),
            "rw": normalize_text(ws.cell(row_idx, 7).value).upper(),
            "esp_rw": normalize_text(ws.cell(row_idx, 8).value).upper(),
            "unit": normalize_text(ws.cell(row_idx, 6).value),
            "existing_ai": normalize_text(ws.cell(row_idx, 17).value).removeprefix("KI:").strip() if normalize_text(ws.cell(row_idx, 17).value).startswith("KI:") else normalize_text(ws.cell(row_idx, 17).value),
        }
        ai_text = generic_ai_for_row(values)
        if ws.cell(row_idx, 17).value != ai_text:
            ws.cell(row_idx, 17).value = ai_text
            updated += 1

    wb.save(path)
    return updated


def main():
    ap = argparse.ArgumentParser(description="Sync MAS-004 master workbooks and enrich KI texts.")
    ap.add_argument("--param-source", default=str(DEFAULT_PARAM_SOURCE))
    ap.add_argument("--param-repo-copy", default=str(DEFAULT_PARAM_REPO_COPY))
    ap.add_argument("--io-source", default=str(DEFAULT_IO_SOURCE))
    ap.add_argument("--io-repo-copy", default=str(DEFAULT_IO_REPO_COPY))
    args = ap.parse_args()

    param_source = Path(args.param_source)
    param_repo_copy = Path(args.param_repo_copy)
    io_source = Path(args.io_source)
    io_repo_copy = Path(args.io_repo_copy)

    if not param_source.exists():
        raise RuntimeError(f"Parameter workbook not found: {param_source}")
    if not io_source.exists():
        raise RuntimeError(f"IO workbook not found: {io_source}")

    updated_source = sync_parameter_workbook(param_source)
    param_repo_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(param_source, param_repo_copy)

    io_repo_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(io_source, io_repo_copy)

    print(f"Parameter workbook synced: {param_source}")
    print(f"Repo workbook copy updated: {param_repo_copy}")
    print(f"KI entries refreshed: {updated_source}")
    print(f"IO workbook copied: {io_repo_copy}")


if __name__ == "__main__":
    main()
