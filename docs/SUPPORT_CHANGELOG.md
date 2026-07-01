# SUPPORT_CHANGELOG - MAS-004_RPI-Databridge

## 2026-07-01 (ESP command short connections)
- Der `EspPlcClient` schliesst den ESP-TCP-Socket nach jeder Antwort wieder, damit kein Raspi-Prozess den Single-Client-Port der ESP32-PLC prozessuebergreifend blockiert.
- Die Reihenfolge bleibt ueber Prozess- und Interprozess-Lock geschuetzt; schnelle Wiederverbindungen werden durch die ESP-Firmware mit kurzer Antwort-Freigabe abgefangen.
- Vor dem lokalen `close()` wird der Socket mit `shutdown(SHUT_RDWR)` beendet, damit die ESP32-PLC das Verbindungsende bei schnellen Kurzverbindungen frueher erkennt.

## 2026-06-29 (Microtom DIClient Adapter Header)
- Ausgehende Raspi -> Microtom Outbox-Requests ergaenzen fuer alle konfigurierten Microtom-Peers zentral den Header `X-DIClient-Adapter-Key`.
- Der Key liegt als `diclient_adapter_key` in der lokalen Raspi-Konfiguration und wird erst unmittelbar beim Senden hinzugefuegt; Outbox-Jobs speichern den Key nicht selbst.
- Die Settings-UI kann den Key setzen/loeschen und maskiert ihn beim Lesen wie ein Secret.
- Der Microtom-Test-Deploy-Helper setzt weiterhin alle externen Systeme auf Simulation und kann den DIClient-Key beim Raspi-only-Deploy in die Testsystem-Konfiguration schreiben.

## 2026-06-24 (MAP0065 fuer physische und virtuelle Taster)
- `MAP0065` sperrt jetzt auch den Safety-/Purge-Reset auf dem gemeinsamen Start/Pause/Reset-Tastelement. Weil Reset physisch auf `raspi_plc21 I0.7` liegt, gilt dafuer das Start-Bit der Maske.
- Die Web-Taste in `/ui/machine-setup/process` und der physische Tasterpfad verwenden dieselbe Freigabelogik: bei gesperrtem Start-Bit ist Reset wirkungslos.
- Die Safety-/Reset-LED-Uebersteuerung auf `Q0.0`/`Q0.2` respektiert `MAP0065`; gesperrte Start/Pause/Reset-Taster bleiben auch physisch dunkel.
- Normale Zustands-LEDs leuchten nicht mehr dauerhaft weiter, wenn ihre zugehoerige MAP0065-Funktion gesperrt ist.
- Die Tasterlampen werden bei jedem Runtime-Zyklus hart physisch synchronisiert, damit alte Ausgangslatches nicht weiterleuchten, wenn der logische LED-Plan bereits `aus` ist.

## 2026-06-22 (Microtom Purge Scenario B)
- `MAS0028=0` beendet den Purge-Prozess nur noch als Microtom/DIClient-origin Kommando.
- `MAS0028=1` von Microtom/DIClient startet einen extern gefuehrten Purge-Prozess und wird nicht mehr als stale Safety-Latch automatisch auf `0` geloescht.
- Bei Microtom `MAS0028=1` werden jetzt ebenfalls stale pending `MAS0028=<state>` Outbox-Callbacks geloescht, damit nach `ACK_MAS0028=1` kein alter `MAS0028=0` Callback mehr zugestellt werden kann.
- ESP-/Device-origin `MAS0028=0` oder `MAS0028=1` wird als Echo des Raspi-authoritativen Zustands quittiert und darf den Purge-Latch nicht setzen oder loeschen.
- Bei Microtom `MAS0028=0` werden weiterhin stale pending `MAS0028=<state>` Outbox-Callbacks geloescht, bevor `ACK_MAS0028=0` gesendet wird.
- Machine-runtime unterdrueckt `MAS0028=0` als eigenen Callback; die Purge-Terminierung bleibt Microtom/DIClient-owned.
- Der Raspi spiegelt den Clear an die ESP32-PLC, damit deren interner Prozess-Purge-Latch geloescht wird, ohne dass die ESP32-PLC selbst ein `MAS0028=0` Richtung Microtom erzeugt.

## 2026-06-18 (Hardware-Reset ueber Raspi I0.7)
- Der Hardware-Reset-/Start-Pause-Taster auf `raspi_plc21 I0.7` loest im Safety-/Purge-/Notstop-Kontext nun denselben Resetpfad aus wie der UI-Reset in `/ui/machine-setup/process`.
- Die Raspberry-PLC-21-Tastereingänge `I0.7` bis `I0.12` werden nun als Analog-/Digital-Eingaenge per `analog_read` mit Schwellwert `raspi_analog_input_high_threshold` ausgewertet. Vorher wurde `I0.7` per `digital_read` gelesen und blieb dadurch softwareseitig `0`; der Resetpfad wurde nicht betreten und `ESP Q0.2` konnte nicht pulsen.
- Die gemeinsame Reset-Sequenz fuer Webinterface und Hardware-Taster pulst `ESP Q0.2` jetzt mit `200 ms HIGH`, `1000 ms LOW`, `200 ms HIGH`, dann LOW.
- Die MachineRuntime aktualisiert im Reset-Kontext den Raspi-PLC-IO-Stand unmittelbar vor der Buttonauswertung, damit ein echter Tasterdruck nicht auf den Hintergrundpoller warten muss.
- Ein gehaltenes `I0.7` darf den Reset nach Cooldown erneut ausloesen, falls eine vorherige Flanke schon als alter Buttonzustand gespeichert war.
- Der nachgelagerte ESP-Befehl `PROCESS RESET` wird bei einem transienten Socket-Timeout einmal wiederholt, damit ein einzelner Kommunikationshaenger den Reset nicht blockiert.
- Der Produktions-Config-Patch setzt `raspi_io_simulation=false`, damit die Raspi-PLC-Eingaenge bei Re-Deploys live bleiben.

## 2026-06-02 (ESP Safety Inputs HIGH=OK)
- Raspi-Runtime interpretiert `ESP32-PLC58 I0.7` und `I0.8` jetzt als Safety-OK-Signale: `HIGH=OK`, `LOW=Fehler aktiv`.
- `I0.7=0` latched `notaus`, `I0.8=0` latched `lichtgitter`; beide setzen weiterhin `MAS0001=21` und `MAS0028=1`.
- Safety-Reset verifiziert nun `ESP I0.7=1` und `ESP I0.8=1`, bevor Prozess- und Motion-Recovery freigegeben werden.

## 2026-06-01 (Absolute ID3-Messfahrt und HMI-Kalibrierung)
- Die ESP-Setup-Messfahrt faehrt ID3 nun ueber absolute AZD-Ziele: `500 mm`, zurueck auf `0 mm`, dann `2500 mm` mit Label-/Schlupf-/Kontrollsensor-Teach und abschliessend absolut auf `erste Labelreferenz - 10 mm`. Am Ende werden ID3 und beide Encoder genullt; der Produktionsstart kennt den 10-mm-Handover.
- Die Wickler-Durchmesserberechnung nutzt weiterhin die vom ESP aufsummierte absolute Wegstrecke `diameter_learn_travel_mm`; die neue absolute Vor-/Ruecksequenz kuerzt diese Basis nicht auf Netto-Endposition.
- Die bisher automatische 2000-mm-ID3-Skalierfahrt wurde aus dem normalen Einrichten entfernt und als geschuetzte HMI-Funktion nach `/ui/machine-setup/calibration` verschoben. Dort koennen Prepare, 2000-mm-Fahrt, Ergebnisanzeige und Anwendung der gemessenen realen Strecke ausgefuehrt werden.
- Neue feste Maschinenparameter: `MAP0077` = Einlaufencoder-Wirkdurchmesser in `1/1000 mm`, `MAP0078` = Auslauf-/ID3-Encoder-Wirkdurchmesser in `1/1000 mm`. Beide sind nicht formatrelevant; `MAP0076` bleibt die feste Label-Laengenkorrektur und steht nach dem aktuellen Maschinenabgleich auf `8` (`+0.8 mm`). Setup und Produktion loesen Label-Laengenfehler nur aus, wenn Rohwert und kompensierter Wert gemeinsam ausserhalb derselben Grenze liegen.
- Produktion gibt bekannte Druckpositionsstopps jetzt als berechnete absolute ID3-Ziele an den AZD-CD aus, nicht mehr als relative Restweg-Positionierung.

## 2026-06-01 (GitHub remotes fuer alle MAS-004-Unterprojekte)
- Fehlende zentrale GitHub-Repos wurden unter `VTI-TSS-CH` angelegt und als `origin` gesetzt:
  - `MAS-004_ESP32-PLC-Firmware`
  - `MAS-004_SmartWickler`
  - `MAS-004_ZBC-Library`
- Der jeweils bereits committed lokale `main`-Stand wurde initial nach GitHub gepusht. Dirty Inbetriebnahme-Aenderungen bleiben lokal unveraendert und wurden nicht automatisch committed.
- `scripts/mas004_multirepo_status.ps1` fuehrt nun auch `MAS-004_SmartWickler`.
- `scripts/mas004_multirepo_sync.ps1` behandelt ESP-Firmware und ZBC-Library nicht mehr als Bundle-only-Repos, da nun zentrale Remotes existieren; SmartWickler ist ebenfalls in der Repo-Liste sichtbar.

## 2026-05-29 (Produktionsfehler Erstkanten-Diagnose)
- ESP-Events vom Typ `production_fault` werden nun in der zentralen Kommunikation als eigene Produktionswarnung protokolliert.
- Bei `label_edge_timeout` zeigt der Log jetzt gefahrenen Suchweg, erlaubten Grenzwert, Timeout sowie Start- und Istpegel von `I0.5`. Damit ist ersichtlich, ob `MAE0027` wirklich Sensorprellen ist oder ob beim Produktionsstart keine neue LOW->HIGH-Labelkante rechtzeitig akzeptiert wurde.

## 2026-05-29 (Einricht-Sensor-Teach vor Motorbewegung)
- Der Raspi setzt den Teach-Eingang des Etikettenerfassungssensors auf Moxa #3 `DIO3` nun synchron fuer `3.5 s` high, setzt ihn danach low und startet erst dann `PROCESS SETUP_MEASURE START` auf der ESP32-PLC.
- Der ESP-Startbefehl enthaelt nun die feste Teach-/Messfahrtsequenz: `TEACH_MS=3500`, `CONTROL_TEACH_MS=3500`, `INFEED_SETTLE_MS=5000`, `CONTROL_POST_TEACH_MS=5000` und `BACKOFF_MM=10.000`.
- Die Encoder-Schlupfdiagnose der langen Teachfahrt nutzt nun mindestens `8.0 mm` Reserve. Das betrifft nur die Setup-Messfahrt; die Druckpositions-Nachkorrektur mit `+/-0.05 mm` bleibt unveraendert.
- Das Wickler-Durchmesserlernen umfasst weiterhin die komplette ESP-Messfahrt. Der Raspi liest `diameter_learn_travel_mm` erst nach abgeschlossenem `PROCESS SETUP_MEASURE`, wendet die Durchmesser an und setzt danach wie bisher die Produktionsbasis inkl. ID3-Position auf `0`.

## 2026-05-26 (Produktionsstart Wickler-Nachpruefung)
- Nach `PROCESS PRODUCTION START` prueft die RPI-Runtime die beiden SmartWickler nach kurzer Anlaufzeit erneut auf `Bereit`/`Warnung`, `continuousModeReady`, `indexedModeEnabled=false`, kein externes Stop-Signal und Wippe im Bereich 8..92 %.
- Wenn ein Wickler direkt nach Start oder waehrend aktivem Produktionslauf aus diesem Continuous-Ready-Fenster faellt, stoppt die Runtime Motor 3 und beide Wickler, setzt den Lauf auf Stoerung und setzt `MAS0028=1`.
- Der Produktions-Master-Payload setzt keine `leaderSpeedMmS`-Vorgabe. Der aktive Produktionsmodus bleibt der lokale Wippen-Continuous-Regler, nicht Indexed; der Raspi gibt nur `indexedModeEnabled=0` und danach `ready&allowMotion=1`.
- Wiederholte Stop-Logs bei dauerhaft gelatchtem `MAS0028` sind unterdrueckt: pro unveraenderter Fehler-Signatur wird der harte Stop nur einmal protokolliert und nicht alle zwei Sekunden erneut.

## 2026-05-23 (Produktionsstart nutzt ESP-Produktionsrunner und Wickler-Continuous)
- Der automatische Start von Motor 3 aus `Pause`/`MAS0002=1` ist wieder freigegeben, aber nicht mehr ueber den alten `PROCESS INDEXED START`-Takt. Der Raspi startet jetzt `PROCESS PRODUCTION START SPEED_MM_S=<MAP0014> RAMP_MM_S2=300`.
- Beide SmartWickler werden vor dem Start in den kontinuierlichen Regler gebracht: `indexedModeEnabled=0`, danach `ready&allowMotion=1`, anschliessend Verifikation von `continuousModeReady`, `lastCommandOk`, alarmfreiem Drive und Wippe im Bereich 8..92 %.
- Beim Stop/Pause/Fault sendet die Runtime jetzt zusaetzlich `PROCESS PRODUCTION STOP`; die Wickler bleiben in `Pause`/`Bereit` im Continuous-ready-Zustand und werden nur bei Stop/Fault in blau/Stop gesetzt.
- Richtungsstandard dokumentiert: Motor 3, Infeed-Encoder und Drive-Encoder zaehlen in Vorzugsrichtung positiv, Rueckspulen ist negativ. Motor 3 behaelt dafuer die Maschinenmapping-Konfiguration `invert_direction=true`; die Encoder werden firmwareseitig an der PCNT/ISR-Eingangsgrenze normalisiert.
- ID3 wird nicht als Positionsachse behandelt: der Motor-Setup-Master spielt fuer ID3 keinen `zero_offset_steps`-Restore mehr zurueck. Die logische ID3-Null wird beim Einrichten/Messfahrtstart und beim `PROCESS PRODUCTION_RESET` vor Pause/Start mit `MOTOR 3 SET_POSITION_MM=0.000` neu gesetzt.

## 2026-05-22 (Produktionsstart startet ESP-Indexed-Takt, durch 2026-05-23 ersetzt)
- Ursache fuer `MAS0001=5`, aber keine Bewegung: Der RPI-Zustandsautomat hat bisher nur den Maschinenzustand nach `Produktionsbetrieb` gesetzt; ein expliziter ESP-Bewegungsauftrag fuer Motor 3/Wickler wurde beim Start aus Pause nicht ausgelöst.
- Historischer Zwischenstand: Beim akzeptierten Start aus `Pause` wurde ein `production_runtime.pending_start` gesetzt. Nach dem Uebergang `4 -> 5` synchronisierte der Raspi zuerst `MAS0001=5` zum ESP, liess dessen Zustandswechsel-Reset ablaufen, setzte die Produktionsparameter, bereitete beide Wickler fuer Indexed-Betrieb vor und startete danach `PROCESS INDEXED START`. Dieser Pfad wurde am 2026-05-23 durch `PROCESS PRODUCTION START` ersetzt.
- Beim Verlassen von Produktion stoppt die Runtime `MOTOR 3`, `PROCESS INDEXED`, `PROCESS PROFILE`, `PROCESS WICKLER` und setzt die Wickler je nach Zielzustand in `Bereit`/Pause oder Stop.
- `MachineRuntime` nutzt nun denselben Produktionslog-Pfad wie `LogStore`, damit Startsperre `MAS0030`/alte Produktionsdateien nicht gegen eine andere State-Datei laufen als die UI/Downloads.
- Nicht-bewegende Maschinenzustaende wie `Pause`/`Produktions-Stop` werden einmalig zum ESP synchronisiert, auch wenn nach einem Service-Neustart kein RPI-Statewechsel stattgefunden hat.

## 2026-05-22 (Positionsachsen: Hardware-MOVE/IN-POS Rueckmeldung)
- ID1/2/4-9 bleiben fuer Zielpositionen und Softlimits protocol-first ueber ESP/AZD Direct-Data.
- AZD `DOUT0 = 134 MOVE` wird als schnelle Hardware-Bewegungsrueckmeldung verwendet; die Runtime liest die verdrahteten ESP-Eingaenge waehrend der Zielpruefung mit.
- AZD `DOUT1 = 138 IN-POS` bleibt fuer ID1/ID2 als hardwareseitiges Positioning-Complete-Signal dokumentiert und kann die Zielpruefung abschliessen, wenn vorher Hardware-MOVE gesehen wurde oder der frische Motorstatus das Zielkommando bestaetigt.
- Fuer ID4-9 ist aktuell nur OUT0 verdrahtet; dort wird MOVE hardwareseitig genutzt, IN-POS bleibt bis zu einer zusaetzlichen OUT1-Verdrahtung protocol-basiert.
- IO-Liste `master_data/SAR41-MAS-004_SPS_I-Os.xlsx` wurde entsprechend von Reserve-Bezeichnungen auf MOVE/IN-POS-Signale aktualisiert.

## 2026-05-21 (Wickler-Sicherheitsabbruch bei Messfahrt geschaerft)
- Der Einricht-Orchestrator bricht Motor-3-Messfahrten nun bereits ab, wenn eine Wicklerwippe in den Sicherheitsrandbereich laeuft (`<=8 %` oder `>=92 %`). Damit wird nicht erst auf roten Wicklerstatus/MAE-Latch gewartet.
- Beim Abbruch wird weiterhin der zentrale Bewegungsstopp ausgefuehrt: ESP-Setup-Messfahrt Stop, Wickler-Cancel, Index/Profile Stop, Motor 3 auf 0 und beide Wickler in Stop.
- Hintergrund zum Fehler `setup_measure_max_forward_exceeded`: Die Ursache liegt firmwareseitig in einer absoluten Max-Forward-Pruefung nach bereits gefundener Etikettenreferenz; der passende ESP-Fix ist im Firmware-Repo dokumentiert und muss mitgeflasht sein.

## 2026-05-21 (Motor-Setup gegen Runtime-Ueberschreiben gehaertet)
- Ursache fuer springende Istpositionen eingegrenzt: Vor manuellen Motorbewegungen wurde bisher der Motor-Setup-Master komplett zurueckgespielt. Dabei wurden auch `zero_offset_steps` bzw. `SET_POSITION_MM` erneut geschrieben, wodurch eine zuvor gesetzte Istposition bei der naechsten Bewegung wieder umdefiniert werden konnte.
- Restore vor Bewegungen schreibt nun nur noch bewegungsrelevante Parameter/Limits, aber keine Positionsreferenz. Die aktuelle Achsposition bleibt damit im ESP/AZD erhalten; `SET_POSITION_MM` passiert nur noch bei ausdruecklicher Positionsuebernahme bzw. bewusstem Positionsrestore.
- Die Motor-Setup-Seite setzt beim Oeffnen/Bedienen eine temporaere Motor-Setup-Sperre. Solange diese aktiv ist, darf die automatische Stop-Positionslogik ID5/6/7/8/9 nicht im Hintergrund auf Stop-Positionen verfahren. Ein echter Maschinenbefehl ueber virtuelle/physische Tasten oder Microtom hebt diese Sperre wieder auf.

## 2026-05-21 (Motor-Setup-Master Positionsrestore)
- Der Motor-Setup-Master schuetzt gespeicherte Achspositionen jetzt gegen falsche Live-Istwerte nach ESP/AZD-Neustart: reine Config-/Restore-Aktionen aktualisieren zwar Motorparameter, ueberschreiben aber nicht mehr den gespeicherten Positions-Snapshot.
- Historischer Hinweis: Der zwischenzeitlich eingefuehrte automatische Positionszaehler-Restore fuer ID1/2/4-9 wurde am 2026-06-25 wieder entfernt und gesperrt. `SET_POSITION_MM`, `ZERO`, `SET_MIN`, `SET_MAX`, `zero_offset_steps`, `min_tenths_mm` und `max_tenths_mm` duerfen fuer Positionsachsen nur noch ueber `/ui/machine-setup/motors` geschrieben werden.
- Motor ID3 bleibt davon ausgenommen, da die Transportachse ihre produktive Nullung im Einricht-/Messfahrprozess erhaelt.

## 2026-06-25 (Positionsachsen gegen DB-/Runtime-Restore gesperrt)
- Ursache fuer ID7-Anschlaglauf live bestaetigt: `motor_setup_position_restored` hatte nach einer Live-Position ausserhalb Min/Max den Positionszaehler aus einem alten `motor_setup_master` per `SET_POSITION_MM` gesetzt und gespeichert. Dadurch konnte die UI wieder plausible Werte anzeigen, obwohl die reale Mechanik nicht mehr dazu passte.
- Runtime und Web-Status/Move-Guard schreiben keine Motor-Setup-Master-Daten mehr auf den ESP zurueck. `motor_setup_master` ist fuer Positionsachsen nur noch Spiegel/Archiv der Motor-Setup-Seite.
- Der Raspi-ESP-Motorclient blockiert geschuetzte Setup-Schreibbefehle fuer ID1/2/4-9, ausser der Aufruf kommt explizit aus der Machine-Setup-Motorseite.
- Wenn ein alter automatischer Positionsrestore neuer ist als die letzte Machine-Setup-Speicherung, blockiert die Runtime automatische Positionsfahrten der betroffenen Achse bis zur erneuten bewussten Kalibrierung/Speicherung in `/ui/machine-setup/motors`.

## 2026-05-21 (Moxa E1211 -> 3x E1213 Source-Ausgaenge)
- Die Feld-IO-Topologie wurde von `2x Moxa ioLogik E1211` auf `3x Moxa ioLogik E1213` umgestellt, damit Source-Ausgaenge verwendet werden koennen.
- Neue ETH1-Adressen: Moxa #1 `192.168.2.102`, Moxa #2 `192.168.2.103`, Moxa #3 `192.168.2.104`, jeweils Modbus/TCP Port `502`.
- IO-Remap in `master_data/SAR41-MAS-004_SPS_I-Os.xlsx`: Moxa #1 fuehrt die frueheren alten Modul-1-Ausgaenge `DO8..DO15`, Moxa #2 die frueheren alten Modul-1-Ausgaenge `DO0..DO7`, Moxa #3 die frueheren alten Modul-2-Ausgaenge `DO0..DO7`.
- E1213-Kanaele werden als `DO0..DO3` plus `DIO0..DIO3` importiert; die DIO-Kanaele werden softwareseitig als Ausgaenge behandelt.
- Statusleuchte liegt neu auf Moxa #3 `DIO0..DIO2`; Teach Etikettenerfassung liegt neu auf Moxa #3 `DIO3`.

## 2026-05-20 (Wickler-Fault stoppt Motor 3 hart)
- Der Einricht-Orchestrator ueberwacht waehrend der sensorreferenzierten Messfahrt beide Smart-Wickler aktiv. Ein roter Wicklerstatus, AZD-Alarm, MAE-Tänzerfehler oder eine Wippe nahe Anschlag (`<=3 %` / `>=97 %`) bricht die Messfahrt sofort ab.
- Beim Abbruch werden `PROCESS SETUP_MEASURE STOP`, `PROCESS WICKLER CANCEL`, `PROCESS INDEXED STOP`, `PROCESS PROFILE STOP`, `MOTOR 3 MOVE_VEL_MM_S=0`, Diameter-Learning-Cancel und `mode=stop` fuer beide Wickler gesendet; `MAS0028` wird gelatcht.
- Die Maschinenruntime sendet dieselbe Bewegungs-Notbremse auch bei sonstigen kritischen roten Fehlern/Purge-Zustaenden, damit Motor 3 und beide Wickler nicht gegeneinander weiterlaufen.

## 2026-05-20 (Setup-Baseline loescht stale Label-Laengenfehler)
- Der Einrichtabschluss loescht nach `PROCESS PRODUCTION_RESET` nun auch Raspi-lokal `MAE0024`, `MAE0025`, `MAE0026`, `MAE0027` und `MAS0028`.
- Damit bleiben Setup-/Messfahrt-Latches wie `Label zu kurz` oder `Label zu lang` nicht mehr als Pause-Blocker stehen, wenn die ESP-Prozessbasis fuer den Produktionsstart bereits zurueckgesetzt wurde.
- Ein akzeptiertes `MAS0002=1` wird nach der Uebernahme konsumiert (`MAS0002=0`), damit derselbe Startbefehl im Uebergangszustand `4` nicht nochmals als unzulaessiger neuer Start geloggt wird.

## 2026-05-20 (Raspi-State gegen stale ESP-Echos gehaertet)
- `MAS0001`, `MAS0002`, `MAS0028` und `MAS0030` sind konsequent Raspi-autoritative Maschinenwerte. ESP-/Wickler-Echos dieser Werte werden quittiert, duerfen den Raspi-Zustand aber nicht mehr ueberschreiben.
- Damit kann ein alter ESP-Mirror von `MAS0028=1` oder `MAS0001=21` den Einrichtprozess nicht mehr nachtraeglich als `Purge active during setup` abbrechen.
- State-Change-Events enthalten jetzt zusaetzlich `purge_active`, `MAS0028`, `critical_reasons` und den aktuellen Safety-Status, damit kuenftige Wechsel auf `21` eindeutig diagnostizierbar sind.
- Die lange Einricht-/Teachfahrt nutzt fuer den kumulativen Encodervergleich nun mindestens `3.0 mm` Schlupfreserve. Die harte Druck-Stop-Nachpositionierung von `+/-0.05 mm` bleibt davon unberuehrt.

## 2026-05-20 (Bahnriss nicht mehr im Einrichten als Purge)
- Bahnriss-/Entnahmesensorik (`ESP I0.4`/`I0.11`, `MAE0008`/`MAE0009`) wird im Einricht-Uebergang und im Einrichtbetrieb (`MAS0001=2/3`) nicht mehr als kritischer Purge-Grund bewertet.
- Hintergrund: Beim Einrichten werden Sensorachsen positioniert und Sensoren geteacht; die Signale duerfen den Wickler-Einrichtworkflow deshalb nicht vorzeitig mit `Purge active during setup` abbrechen.
- Die Ueberwachung bleibt nach abgeschlossenem Einrichten in den produktionsnahen Betriebsarten aktiv.

## 2026-05-20 (Produktions-Bypass und Einrichtabschluss-Baseline)
- Machine Control / Prozesssicht enthaelt jetzt eine Bypass-/Simulation-Karte fuer `MAP0035` bis `MAP0038` inklusive Simulationsparameter `MAP0067` bis `MAP0070`.
- Bypass-Werte werden ueber denselben Routerpfad wie Microtom geschrieben, damit Parameter-DB, Outbox/Audit und ESP32-PLC-Spiegelung konsistent bleiben.
- Neue Parameter: `MAP0067` Materialkamera-Simulation, `MAP0068` Verifikationskamera-Simulation, `MAP0069` Laser-Bypass-Druckdauer und `MAP0070` TTO-Bypass-Druckdauer.
- Der Einrichtworkflow setzt vor dem internen Wechsel nach `Pause` die produktive Prozessbasis zurueck: Label-Schieberegister leer, Prozesslatches zurueckgesetzt und Motor 3 als neuer `0.000 mm` Bezugspunkt uebernommen.

## 2026-05-20 (Maschinenstatus Pause / Einrichten-Gating)
- `Pause` (`MAS0001=7`) erlaubt auf der UI, den physischen Tasten und dem Microtom-Kommandopfad nur noch `Start` und `Stop`.
- `Einrichten` (`MAS0002=3`) ist nur noch aus `Produktions-Stop` (`MAS0001=9`) erlaubt; ein erneutes Einrichten aus `Pause` wird bewusst blockiert.
- Der erfolgreiche Einrichtabschluss schaltet intern auf Zielzustand `Pause`, setzt `MAS0002` aber wieder auf `0`, damit kein stale `MAS0002=7` im Uebergangszustand `2` als unzulaessiges externes Pause-Kommando geloggt wird.
- Laufende Uebergangszustaende behalten ihr internes Ziel bei, auch wenn `MAS0002` nach dem Trigger wieder auf `0` steht.

## 2026-05-20 (Audit-Anzeige lokal leeren)
- Machine Control / zentrale Kommunikationssicht hat nun einen Button `Anzeige leeren`.
- Der Button setzt nur im Browser eine lokale Anzeigegrenze; die Audit-Eintraege bleiben serverseitig gemaess Retention gespeichert und koennen mit `Verlauf wieder anzeigen` wieder in die Ansicht geholt werden.
- Die gespeicherten Audit-UI-Praeferenzen behalten Retention, Fenster und Limit und ueberschreiben die lokale Anzeigegrenze nicht mehr versehentlich beim automatischen Refresh.

## 2026-05-20 (Machine-Control Auditfilter)
- Die zentrale Kommunikationssicht in Machine Control hat nun Filter fuer Richtung (`IN`/`OUT`), Level (`Error`/`Warning`/`Info`), Kategorie (`Kommunikation`/`Maschine`/`Label`) und Freitextsuche.
- Fehler werden hellrot und Warnungen hellgelb hinterlegt. Neben echten `ERR`/`WARNING`-Levels werden auch typische Kommunikationsfehler wie `NAK`, `timeout`, `failed` und `Fehler` lesbar markiert.

## 2026-05-19 (Wickler-Messfahrt erst nach echter Motion-Freigabe)
- Nach dem Wickler-Einmessen reicht `Bereit/Warnung` als Textstatus nicht mehr aus, um Motor 3 fuer die Messfahrt zu starten. Der Raspi prueft jetzt zusaetzlich, dass `externalStopActive=false`, der AZD-Continuous-Speed-Pfad bereit ist und der letzte Wicklerbefehl OK war.
- Vor der sensorreferenzierten Messfahrt sendet der Raspi an beide Wickler explizit `/api/mode mode=ready&allowMotion=1`. Das loest den internen AZD-STOP-Hold fuer den kontinuierlichen lokalen Wippenregler, ohne den normalen Stop-Modus aufzuweichen.
- `start_diameter_learning` wird erst nach dieser Motion-Ready-Pruefung ausgefuehrt. Dadurch kann Motor 3 nicht mehr losziehen, waehrend ein Wickler formal gruen wirkt, aber wegen aktivem STOP nicht nachregelt.

## 2026-05-19 (Einrichten: Formatachsen vor Wickler-Messfahrt)
- Der Einrichtworkflow positioniert vor der Wickler-Messfahrt alle formatrelevanten Achsen ID5/6/7/8/9 als gemeinsamen ESP-Positionssatz. Der ESP laedt die AZD-Direktdaten zuerst in alle Achsen und triggert danach die Bewegungen; es wird nicht mehr achsweise auf Zielerreichung gewartet.
- Die Zielwerte stammen aus dem aktuellen Raspi-Formatplan (`MAP0001`, `MAP0008`, `MAP0009`, `MAP0010` plus Korrekturen `MAP0029` bis `MAP0033`). Vor jeder Bewegung wird gegen die aktiven ESP-Motor-Min/Max-Grenzen geprueft.
- Die lange Einricht-Messfahrt nutzt fuer den Vergleich Einlaufencoder zu Antriebsencoder eine eigene robuste Mindest-Schlupfgrenze von `2.0 mm`. `MAP0040` bleibt die Etikettenlaengen-Toleranz; die harte Druck-Stop-Nachpositionierung bleibt separat bei `+/-0.05 mm`.
- Der exakte `PROCESS SETUP_MEASURE START ...` Befehl wird im Audit-Log protokolliert, damit Einrichtabbrueche wie Slip-/Teach-Fehler direkt mit den gesendeten Toleranzen und Fahrparametern nachvollziehbar sind.
- Bei jedem Einrichtabbruch prueft der Wickler-Orchestrator laufend `MAS0001`/`MAS0002`/`MAS0028`. Sobald der Einrichtzustand verlassen wird, werden `PROCESS SETUP_MEASURE STOP`, `PROCESS WICKLER CANCEL`, `PROCESS INDEXED STOP`, `PROCESS PROFILE STOP`, Diameter-Learning-Cancel und `mode=stop` fuer beide Wickler erzwungen. Dadurch koennen keine vorbereiteten Takt-/Messfahrkommandos nach einem Abbruch weiterlaufen.
- Wickler-Einmessen und die 1000-mm-Vor-/Rueck-Messfahrt bleiben fester Bestandteil jedes `Einrichten`-Aufrufs; gespeicherte Durchmesser oder alte Messergebnisse kuerzen den Ablauf nicht ab.
- Wenn Motor 3 den Operation-Data-Messfahrbefehl akzeptiert, aber keine Bewegung startet, fuehrt der Raspi genau einen kontrollierten Retry mit vorherigem `RESET_ALARM`/`RECOVER_ETO` aus. Teilfahrten werden weiterhin nicht blind wiederholt.
- Motor-Setup ist fuer ID1/2/4-9 die Masterquelle: `Parameter speichern`, `Min setzen`, `Max setzen`, `Nullpunkt setzen` und `Istposition uebernehmen` fuehren jetzt immer ESP-`SAVE`, frischen Refresh, Verifikation und Sync in Parameter-DB plus Master-Excel aus. Reine Parameter-/Limit-Speicherungen ueberschreiben keine Positionsdefaults mehr; Positionen werden nur noch bei explizitem `Istposition uebernehmen` oder `Nullpunkt setzen` als Default fortgeschrieben.
- Produktionsstand nachgezogen: ID7 steht wieder bei `-20.0 mm`, ID8/ID9 stehen bei `100.0 mm`; die Max-Grenzen von ID8/ID9 sind dauerhaft `1000` 1/10 mm und in ESP, DB, Produktions-Excel sowie Repo-Excel synchronisiert.

## 2026-05-18 (Bahnriss nur im Prozessfenster)
- Bahnriss Einlauf/Auslauf (`ESP I0.4`/`I0.11`, `MAE0008`/`MAE0009`) blockieren Reset, Not-Stop und Produktions-Stop nicht mehr.
- Die beiden Signale werden erst nach dem Einrichten in den produktionsnahen Betriebsarten bewertet und bleiben bis inklusive Rueckspulen aktiv.
- Device-/ESP-Pushes `MAE0008=1` oder `MAE0009=1` werden ausserhalb dieses Prozessfensters mit `ACK_...=0` quittiert und nicht als Purge reaktiviert.
- Hintergrund: In Stop/Not-Stop koennen die Bahnriss-Sensoren noch nicht in Produktionsposition sein; ihre Meldung darf deshalb keinen Reset verhindern.

## 2026-05-18 (Stop-Modus Achs-Positionssatz)
- Beim Eintritt in `MAS0001=9` / Produktions-Stop sendet die Runtime jetzt einen definierten Positionssatz an die ESP-Motorsteuerung: ID5 Materialkamera auf `0 mm`, ID6/ID7 Sensorachsen auf `-20 mm`, ID8/ID9 Etikettenanschlaege auf `100 mm`.
- Auch der Stop-/Reset-Positionssatz nutzt jetzt den ESP-Satzbefehl `MOTOR MOVE_ABS_SET ...`, damit Anschlaege, Sensorachsen und Kamera nicht mehr sichtbar nacheinander losfahren.
- Die Motor-Setup-Seite ist fuer ID1-9 die Masterquelle der Inbetriebnahmeparameter. Ein `Parameter speichern` schreibt die ESP-Konfiguration nun auch in Parameter-DB und maschinenlokale Master-Excel, damit alte Importwerte keine Softlimits oder Defaults zuruecksetzen. Die Git-Repo-Excel bleibt ueber Release-Commits versioniert.
- Der Positionssatz wird pro Stop-Eintritt idempotent gesendet und bei Fehlern nur noch maximal dreimal mit 60 Sekunden Abstand erneut versucht; er wird nicht bei jedem UI-/Status-Refresh dauerhaft wiederholt, damit der Motorbus nicht unnoetig belastet wird.
- `ACK_MOVE_ABS_MM` allein gilt nicht mehr als erledigt: Die Runtime refreshed die betroffenen Achsen nach dem Befehl und markiert den Stop-Positionssatz nur als `ok`, wenn die Achse am Ziel ist oder eine echte Bewegung meldet. Vor jedem Stop-Positionsbefehl werden `RESET_ALARM` und `RECOVER_ETO` fuer die jeweilige Achse ausgefuehrt.
- Die Stop-Positions-Logikversion wurde angehoben, damit Produktions-Raspis alte fehlgeschlagene Versuche nach dem ESP-Direct-Data-Fix sofort neu bewerten und nicht auf die alte Retry-Sperre warten.
- Safety-/Purge-Reset nutzt fuer Motor 3 jetzt dieselbe Operable-Semantik wie die Wickler-Messfahrt: Link OK, kein Alarm und kein HWTO reichen fuer die Reset-Verifikation, weil der Etikettenantrieb ueber den Hardware-START/STOP-Pfad laeuft und das AZD-READY-Bit dort nicht stabil als Reset-Kriterium taugt. Fuer ID1/2/4-9 bleibt `ready=true` weiterhin Pflicht.
- Die Wickler-Reset-Verifikation bewertet jetzt den geforderten sicheren Stop-Zustand: `online`, kein Alarm, keine Bewegung, `modeLabel=Stop` und ein unkritischer Fault-Text reichen auch dann, wenn das AZD-READY-Diagnosebit im Stop-Modus `false` meldet. Der Reset startet weiterhin keine Wickler-Regelung.
- Die Stop-Positions-Logikversion wurde auf `7` erhoeht. Die Verifikation zaehlt `moving=true` nicht mehr sofort als Erfolg, sondern pollt bis Ziel erreicht, Stillstand ausserhalb Ziel, Alarm oder Timeout. Damit werden Achsalarme wie bei ID7 nicht mehr durch einen zu fruehen Erfolg verdeckt.

## 2026-05-18 (Wickler-Messfahrt: Motor-3-Referenz und AZD-Operable-Gate)
- Der interne Setup-Wicklerworkflow setzt Motor 3 nach erfolgreichem Wickler-Einmessen explizit mit `RESET_ALARM`/`RECOVER_ETO` in einen fahrbaren Zustand. Das AZD-`ready`-Bit wird nur noch diagnostisch verwendet, weil es am Produktionsstand nicht bei allen AZD-Konfigurationen stabil gemappt ist; harte Sperren bleiben Linkfehler, Alarm und HWTO.
- Erst danach wird die aktuelle physische Position mit `MOTOR 3 SET_POSITION_MM=0.000` als neuer Messfahrt-Nullpunkt uebernommen.
- Die Stop-Toleranz `+/-0.05 mm` wird nicht am Start der Messfahrt bewertet, sondern nach der 1000-mm-Vorwaertsfahrt und nach der Rueckfahrt auf den neuen Nullpunkt. Pro Stopp bleiben maximal drei Nachkorrekturen erlaubt.
- Die Stop-Toleranz von Motor 3 wird fuer die Nachkorrektur auf Rohstep-Ebene (`feedback_steps`/`command_steps` plus `steps_per_mm`) bewertet. Die 1/10-mm-Anzeigewerte sind zu grob fuer `+/-0.05 mm`; kleine Restfehler werden deshalb mit `MOTOR 3 MOVE_REL_STEPS=<n>` korrigiert.
- Die Rohstep-Bewertung verfolgt den festen Zielpunkt aus `target_tenths_mm` und `zero_offset_steps`, nicht den nach einer Relativkorrektur weitergeschobenen AZD-`command_steps`-Wert. Dadurch wird eine erfolgreiche kleine Nachpositionierung nicht faelschlich weiter als `0.095 mm` Restfehler gewertet.
- Die 1000-mm-Messfahrt nutzt `MOTOR 3 MOVE_REL_MM_OP=<mm>` und damit den ESP-Setup-Fahrpfad mit Operation-Data-START. `MOVE_REL_MM` bleibt fuer den spaeteren hardware-synchronen Motor-3-Taktpfad reserviert; die Einrichtfahrt darf nicht vom Hardware-START-Mapping abhaengen.
- Abwickler und Aufwickler erhalten `stop`/`resetAlarm`/`etoRecovery`/`calibrate` nun phasenweise parallel, damit das Einmessen beider Wippen zeitgleich startet und keine kuenstliche 2-3-s-Verzoegerung zwischen den Wicklern entsteht.

## 2026-05-11 (Machine Control Purge/Safety Anzeige getrennt)
- Machine Control unterscheidet die rote Kopfstatus-Anzeige jetzt zwischen echtem `Purge` (`MAS0028`/`purge_active`) und `Safety/Reset` (Safety-Latch oder Maschinenstatus `21`).
- Hintergrund Produktionsbefund: `MAS0028=0` und `critical_reasons=[]`, aber ein fehlgeschlagener Motor-Ready-Reset hielt `Safety-Latch=true`. Das ist kein aktiver Purge, sondern ein noch nicht abgeschlossener Reset-/Motion-Recovery-Zustand.

## 2026-05-11 (Reset setzt Motoren wieder Ready)
- Safety-/Purge-Reset fuehrt fuer die neun ESP32-PLC Oriental-Achsen jetzt nach der ESP-Resetsequenz immer `MOTOR APPLY_ETO_RECOVERY`, `MOTOR RECOVER_ETO`, pro Achse `RESET_ALARM` und pro Achse `RECOVER_ETO` aus.
- Die finale Reset-Entscheidung basiert nicht mehr auf einem einzelnen transienten Recover-ACK, sondern auf einer mehrfachen `MOTOR <id> REFRESH`-Verifikation: Link OK, Drive ready und kein Alarm.
- Die Verifikation wertet zusaetzlich die neuen AZD-Monitorfelder `0179`, `017B`, `017D` aus. Damit kann ein Drive als ready erkannt werden, auch wenn der physische R-OUT READY-Ausgang nicht auf dem bisher verwendeten Bit liegt.
- Fehlerdetails im Reset enthalten nun `input_raw`, `output_raw`, `monitor0179`, `monitor017B`, `mps`, `mbc` und `hwto`, damit HWTO-/Brake-/Safety-Zustaende direkt diagnostizierbar sind.

## 2026-05-11 (Reset loescht Purge-Latch frueher)
- Safety-/Purge-Reset loescht `MAS0028` und resettable Safety-Fehler jetzt direkt nach quieten Safety-Eingaengen und erfolgreichem ESP `PROCESS RESET`.
- Eine nachfolgend fehlgeschlagene Motor-/Wickler-Recovery haelt damit keinen alten `MAS0028=1` Purge-Latch mehr fest, solange kein echter kritischer Grund mehr aktiv ist.
- Bleiben echte kritische Gruende aktiv, zum Beispiel Bahnriss-Eingaenge oder nicht ruecksetzbare MAE-Fehler, wird `MAS0028=1` weiterhin korrekt reasserted.
- Regressionstest ergaenzt: Reset mit geloeschtem kritischem Grund, aber simulierter Motion-Recovery-Stoerung, muss `MAS0028=0` setzen.

## 2026-05-11 (Produktions-Raspi Microtom-Outbox HTTP/HTTPS bereinigt)
- Aktuelle neue Primary-Sends an Microtom funktionieren ueber `http://10.141.94.202:5000/api/inbox` mit HTTP 200.
- Ursache fuer die haengende Outbox waren 38 alte Queue-Eintraege mit Ziel `https://10.141.94.202:5000/api/inbox`; Microtom spricht auf Port 5000 HTTP, daher scheiterten diese Eintraege mit `SSL: WRONG_VERSION_NUMBER`.
- Vor der Runtime-Korrektur wurde auf dem Produktions-Raspi die Sicherung `/var/lib/mas004_rpi_databridge/databridge.db.bak_peerfix_20260511_144136` erstellt.
- Die betroffenen Alt-Eintraege wurden auf `http://10.141.94.202:5000/api/inbox` umgeschrieben und erneut eingeplant; danach wurden alle erfolgreich quittiert und die Outbox war leer.

## 2026-05-11 (Wickler-Reset bleibt bewegungsarm)
- Sicherheits-/Purge-Reset sendet an Abwickler und Aufwickler nur noch `stop`, `resetAlarm`, `etoRecovery`, `stop`.
- Der Reset setzt die Wickler damit hardwareseitig frei, startet aber keinen `ready`-Regelmodus mehr.
- Die Reset-Pruefung akzeptiert eine unten/oben stehende Wippe als sicheren Stop-Zustand, solange AZD online, ready und alarmfrei ist.
- Einmessen und 1000-mm-Messfahrt bleiben ausschliesslich Einrichtkontext (`Einrichten`/`MAS0002=3` bzw. der interne Setup-Wicklerworkflow nur im Setup).

## 2026-05-11 (Wickler-Einmessen nur im Einrichtkontext)
- Der interne Setup-Wicklerworkflow ist Teil des produktiven Einrichtablaufs und startet Wickler-Einmessen plus 1000-mm-Messfahrt nicht mehr frei aus jedem Maschinenzustand.
- Bei aktivem Purge/Not-Stop oder ausserhalb von `MAS0002=3` bzw. Maschinenzustand `2/3` wird der interne Setup-Wicklerworkflow abgewiesen und als Einrichtfehler protokolliert.
- Damit kann ein Reset-/Purge-Ablauf nicht versehentlich Wicklerbewegungen oder Messfahrten ausloesen; Einmessen gehoert zur Einrichten-Taste bzw. zum Einrichtmodus.

## 2026-05-11 (Production Microtom Peer Topology)
- Documented and staged the current production peer topology:
  - primary Microtom/DIClient peer: `http://10.141.94.202:5000`
  - optional engineering laptop Microtom simulator/testtool: `https://10.141.94.212:9090`
  - watchdog host: `10.141.94.202`
- Updated the 10.141.94 commissioning topology/config patch so future production applies do not accidentally promote the engineering laptop testtool to the primary peer.
- Operational note: the secondary peer is diagnostic-only and may be offline without blocking the primary Microtom callback lane.

## 2026-05-11 (MOXA/Statusleuchte entkoppelt)
- MOXA-Modbus/TCP-Zugriffe werden pro Endpoint `host:port` serialisiert, damit IO-Refresh und Statuslampen-Schreibzugriffe nicht mehr parallel auf dasselbe ioLogik-Modul laufen.
- Unveraenderte Ausgangswerte werden nicht mehr erneut auf die Hardware geschrieben, solange der letzte Wert bereits live/simuliert bekannt ist.
- Die Maschinen-Statusleuchte nutzt jetzt einen best-effort-Schreibpfad mit kurzem Fehler-Cooldown. Ein temporaerer MOXA-Timeout blockiert dadurch nicht mehr den Maschinenruntime-Zyklus und kann die ESP32-PLC/Motor-Kommunikation nicht mehr indirekt ausbremsen.
- Produktionsbefund vor dem Fix: MOXA #1 und #2 waren per Ping und TCP/502 erreichbar, direkte Modbus-Reads/Writes funktionierten, aber parallele Runtime-Zugriffe konnten in Timeouts laufen.
- Regressionstests fuer unveraenderte Ausgangswerte und best-effort MOXA-Schreibfehler ergaenzt.

## 2026-05-11 (Safety Reset / MAE0008-MAE0009 Clear)
- Machine Control zeigt den kombinierten Start/Pause-Taster im Safety-/Purge-Kontext jetzt eindeutig als `Reset`; nach erfolgreichem Reset faellt er wieder auf `Start` bzw. `Pause` zurueck.
- Der Resetpfad bewertet nach erfolgreicher ESP-Resetsequenz und Motor-/Wickler-Ready-Verifikation den aktuellen IO-/Fehlerzustand neu.
- Die gelatchten Etikettenfuehrungsfehler `MAE0008` und `MAE0009` werden nur dann automatisch auf `0` gesetzt, wenn die zugeordneten ESP-Eingaenge `I0.4` bzw. `I0.11` nach dem Reset ruhig sind.
- Bleibt einer dieser Eingaenge aktiv, bleibt auch der Fehler aktiv und die Maschine bleibt korrekt in `MAS0001=21` / `MAS0028=1`.
- Produktionsdiagnose nach Deploy: `ESP I0.4`, `I0.7`, `I0.8` und `I0.11` waren live LOW; die alten Latches `MAE0008=1` und `MAE0009=1` waren noch aktiv und werden erst durch einen neuen Resetlauf bewertet/geloescht.
- Regressionstests fuer beide Resetfaelle ergaenzt.

## 2026-05-11 (Machine Control Lesbarkeit Purge-Gruende)
- Die Machine-Control-Seite stellt `Kritische Gruende` jetzt als umbrechende, lesbare Chips mit Klartext und Code dar, statt die Liste in einer einzelnen Monospace-Zeile abzuschneiden.
- `MAP0065` wird mit erzwungenem Word-Wrap angezeigt, damit lange JSON-Freigabemasken die Karte nicht mehr ueberlaufen lassen.
- Produktionsdiagnose: Der aktuell aktive Purge kommt nicht von Not-Aus oder Lichtgitter, sondern von aktiven Bahnriss-Eingaengen `ESP I0.4` und `ESP I0.11` sowie gespeicherten Fehlerbits `MAE0008=1` und `MAE0009=1`.

## 2026-05-11 (ESP/Motor-Kommunikation gehaertet)
- Ursache fuer die wiederkehrenden Motor-Kommunikationsaussetzer eingegrenzt: Der ESP32-PLC/W5500-Endpunkt ist ein kurzlebiger Single-Client-Socket, waehrend der Raspi-Client den Socket bisher bis zu 40 Requests halb-persistent hielt.
- `EspPlcClient` schliesst den ESP-Kommandosocket jetzt nach jeder Antwort bewusst sauber. Damit passt der Raspi wieder zum Firmware-Vertrag und vermeidet stale/halb-offene TCP-Fenster bei Modbus-RTU-Refreshes und ESP-Push-Bursts.
- Der per-Motor-Refresh auf `/ui/machine-setup/motors` ist jetzt serverseitig entprellt: parallele oder sehr schnelle Refreshes desselben Motors werden aus einem kurzen Cache bedient, statt mehrere teure `MOTOR <id> REFRESH`-Modbus-Lesezyklen auf den ESP zu stapeln.
- MOXA-Modbus/TCP nutzt nun einen eigenen kurzen lokalen Timeout (`moxa_timeout_s`, Default 1.5 s) statt des allgemeinen 10-s-HTTP-Timeouts. Dadurch koennen traege oder kurz nicht erreichbare MOXA-Module den Maschinenloop nicht mehr mehrere Sekunden blockieren.
- LED-/Statuslampen-Schreibfehler werden im Maschinenloop abgefangen und protokolliert, ohne den restlichen Runtime-Zyklus abzubrechen.
- Diagnosebefund am Produktionssystem: ICMP zu `192.168.2.101` war stabil, aber `eth1` hatte TX-Errors und direkte Raw-Socket-Tests zeigten sporadisch Timeout/Connection-Refused-Fenster. Produktionszugriffe muessen weiterhin ueber den Raspi-Client laufen, nicht ueber parallele Raw-Socket-Stresstests.
- Regressionstest ergaenzt, der sicherstellt, dass `EspPlcClient` kurzlebige Verbindungen verwendet.

## 2026-05-11 (Motor Setup Fehlerbehandlung)
- Fixed recurring `500 Internal Server Error` responses on `/ui/machine-setup/motors` when the ESP32-PLC motor endpoint temporarily refused a connection during `MOTOR <id> REFRESH` or another motor command.
- Motor setup endpoints now convert ESP/TCP communication failures into structured `502 Bad Gateway` API responses with a readable detail message instead of leaking an uncaught Python traceback.
- The UI can therefore keep the motor page alive and show the motor communication error in the card/status area while the ESP endpoint recovers.
- Added a regression test that verifies a refused ESP motor refresh returns `502` and not `500`.

## 2026-05-09 (Machine Control / Audit Log)
- Reworked protected `/ui/machine-setup/process` into a Machine Control / Audit page.
- Added virtual buttons for Start/Pause, Stop, Einrichten, Synchronisieren, Leerfahren and Zurueckspulen. They use the same `MAS0002`, state and `MAP0065` permission logic as the physical Raspi PLC buttons.
- Added `/api/machine/button` for protected virtual button actions.
- Added a central human-readable audit view combining Raspi/Microtom communication logs, device traffic, machine events and label events with code metadata from the parameter master.
- Added audit logfile download and configurable detailed audit retention in hours through `/api/machine/audit*` and Settings.

## 2026-05-09 (Motor Setup Fahr-/Haltestrom)
- Extended `/ui/machine-setup/motors` with separate fields for `Fahrstrom [%]` and `Haltestrom [%]`.
- `current_pct` remains the backwards-compatible Fahrstrom/Base-current value used for motion commands.
- New `hold_current_pct` is sent to the ESP32-PLC as the Haltestrom/Stop-current value.
- `Parameter speichern` now persists both current values through the ESP `MOTOR <id> SAVE` path so the AZD drive current settings and ESP motor config stay aligned.

## 2026-05-08 (Motor Setup Absolute Position)
- Added absolute-position commissioning controls to `/ui/machine-setup/motors`.
- New field `Istposition setzen [mm]` assigns the current physical axis position to an entered absolute millimeter value and saves the resulting ESP motor offset persistently.
- New field `Absolut fahren nach [mm]` sends `MOVE_ABS_MM` so a motor can be driven directly to an entered absolute position.
- Added Raspi API wrapper `/api/motors/{motor_id}/position` and ESP motor client support for `MOTOR <id> SET_POSITION_MM=<mm>`.

## 2026-05-08 (MAS0028 Purge Clear Anti-Echo)
- Fixed a Purge echo/race where `MAS0028=0` from Microtom/DIClient could be followed by an older or stale `MAS0028=1` callback.
- Deduplicated machine/device status callbacks now use a latest-state-wins queue mode, so stale pending status values cannot overtake a newer state value after peer downtime or retries.
- A successful Microtom write `MAS0028=0` now removes pending `MAS0028=<state>` callbacks before the ACK is queued, while keeping the actual `ACK_MAS0028=0` response.
- Immediate ESP/device-origin `MAS0028=1` echoes are ignored for a short grace window after an external clear; hard active safety/critical causes are still allowed to reassert `MAS0028=1` through the machine runtime.
- Added regression tests for Outbox status replacement, Microtom purge clear cleanup, ESP stale-echo suppression and runtime clear marking.

## 2026-05-08 (Production Logfile Ready-State Self-Healing)
- Fixed a stale production-log state where UI could show `Files ready=yes` while the production file list was empty and `MAS0030=?` returned `0`.
- `MAS0002=1` is now blocked only when real downloadable production logfiles exist. A stale `_production_state.json` with `ready=true` but no files is self-healed to `ready=false` and `MAS0030=0`.
- `MAS0030=?` now reconciles the production-log state before answering, so Microtom sees the same state as `/api/production/logfiles/list`.
- Start/stop lifecycle lines are written directly by `ProductionLogManager`, so even a quiet production creates at least `gesamtanlage_<label>.txt`.
- If stopped production files exist but `MAS0030` was stale at `0`, the manifest recovers `MAS0030=1` and keeps the next start blocked until the files are downloaded.
- Added regression tests for stale-ready cleanup, stopped-file recovery and quiet-production logfile creation.

## 2026-05-01 (Safety-Reset validiert echte Motor-Ready)
- Safety-/Not-Aus-Reset meldet erst dann Erfolg, wenn nach `MOTOR APPLY_ETO_RECOVERY`, `MOTOR RECOVER_ETO` und `RESET_ALARM` alle ESP-AZD-Motoren `1..9` live verifiziert `ready=true` und `alarm=false` melden.
- Beide Smart-Wickler werden nach `stop`, `resetAlarm`, `etoRecovery`, `ready` ebenfalls ueber `/api/state` verifiziert. `HWTO/STO aktiv`, `ready=false` oder ein Wickler-Fault bleibt dadurch als Reset-Fehler sichtbar.
- `MAS0002=2` und der Raspi-Taster `I0.7` koennen den Reset nun auch aus einem halb zurueckgesetzten Purge-/Fehlerzustand erneut starten. Wiederholtes Senden von `MAS0002=2` wird anhand des Parameter-Update-Zeitstempels als neuer Reset-Versuch erkannt, ein unveraenderter alter Wert wird nicht endlos wiederholt.
- Resettable Safety-Fehler (`MAS0028`, `MAE0001`, `MAE0024`, `MAE0027`, `MAE0030`, `MAE0034`) werden nur nach erfolgreicher Ready-Verifikation geloescht.

## 2026-04-30 (eth1 ESP32-PLC-Kommunikation robuster)
- Raspi-seitiger ESP-TCP-Client serialisiert Zugriffe pro `host:port` jetzt ueber einen Endpoint-Lock. Dadurch konkurrieren Motor-UI, IO-Snapshot, Setup-Orchestrierung und Parameter-Mirroring nicht mehr gleichzeitig um den Single-Client-W5500-Socket der ESP32-PLC.
- Nach ESP-Verbindungsfehlern gibt es einen kurzen exponentiellen Cooldown statt sofortiger Retry-Stuerme auf `192.168.2.101:3010`.
- ESP-spezifische Timeouts wurden von `http_timeout_s` getrennt:
  - `esp_connect_timeout_s = 1.5`
  - `esp_read_timeout_s = 2.0`
  - `esp_command_timeout_s = 8.0`
- Wenn diese neuen Felder in einer bestehenden Runtime-Config fehlen oder `null` sind, verwendet der Code die neuen kurzen Defaults und faellt nicht mehr auf den alten allgemeinen `http_timeout_s` zurueck.
- Schreibende ESP-Kommandos werden nicht automatisch mehrfach wiederholt, damit keine Relativfahrten oder Setzbefehle doppelt ausgefuehrt werden. Nur Read-Pfade erhalten einen kurzen, kontrollierten Retry.
- ESP-Push-Reads fuer `MA*`-Parameter werden nun lokal aus der Raspi-Parameterdatenbank beantwortet. Damit entsteht kein reentrant TCP-Callback zum ESP, waehrend der ESP auf die Push-Antwort wartet.
- Grosse JSON-Antworten wie `MOTOR LIST?`/Status-Snapshots nutzen explizit groessere Read-Limits; normale Kurzbefehle bleiben klein und schnell.
- Tests ergaenzt:
  - `tests/test_esp_plc_client.py`
  - `tests/test_esp_push_listener.py`

## 2026-04-30 (Oriental-Motorstatus ohne blockierendes ESP-Auto-Polling)
- Ursache fuer `link:false` auf `/ui/machine-setup/motors` eingegrenzt: Der ESP32-PLC-Motor-Manager startet bewusst mit `MOTOR POLL=0`, damit fehlende oder langsame AZD-Teilnehmer den ESP-TCP-Endpunkt nicht blockieren.
- `MOTOR POLL=1` wurde live testweise aktiviert; die ersten AZD-IDs antworteten darauf, aber der ESP-TCP-Endpunkt blockierte waehrend des RTU-Polls zeitweise. Auto-Poll wurde deshalb wieder auf `MOTOR POLL=0` gesetzt.
- Die Raspi-Motors-UI fragt den ESP nun nicht mehr ueber den grossen Sammelbefehl `MOTOR LIST?` ab, sondern ueber kleine Einzelstatus-Frames. Das verhindert gekappte JSON-Antworten und reduziert die Last.
- Pro Motor gibt es jetzt einen expliziten `Status aktualisieren`-Pfad (`MOTOR <id> REFRESH`), der genau diesen einen AZD ueber Modbus RTU live liest und die Werte cached.
- Nach Verifikation am echten ESP wurde die Uebersicht weiter entschaerft: Auto-Refresh liest nur noch Cache plus `MOTOR POLL?`; auch kleine 9-fache `STATUS?`-Abfragen waren fuer den ESP/W5500-Single-Socket zu viel.
- Die Uebersichtsseite zeigt den Auto-Poll-Zustand an; fuer IBN/Service bleibt manuelles Refresh der bevorzugte Weg, solange der ESP parallel Prozess-/Wicklersteuerung macht.

## 2026-04-30 (Setup-Orchestrierung: Wickler-Indexed sauber verlassen)
- Der interne Setup-Wicklerworkflow schaltet die Wickler vor Reset/ETO/Einmessen aus dem Indexed-Modus, damit keine alte Taktfahrt in die Messfahrt hineinwirkt.
- Service-Stop und Einrichtablauf setzen beide Wickler deterministisch auf `indexedModeEnabled=0` und senden danach `/api/mode stop`.
- Hintergrund: Nach Takt-/Messabbruechen konnte ein Wickler mit alter Indexed-Konfiguration im Drive-MOVE-Zustand haengen bleiben; der Raspi bereitet die Wickler nun vor Service-/Kontinuierlichfahrten deterministisch vor.

## 2026-04-30 (Produktions-Raspi mit IBN-Stand deployed)
- Produktions-/IBN-Raspi `10.141.94.213` wurde mit dem damaligen Raspi-IBN-Stand aktualisiert.
- Runtime-Settings unter `/etc/mas004_rpi_databridge/config.json` wurden nicht ueberschrieben.
- Masterliste wurde nach `/var/lib/mas004_rpi_databridge/master/Parameterliste_master.xlsx` kopiert und in die SQLite-Parameterdatenbank importiert.
- Importergebnis auf dem Raspi: `inserted=21`, `updated=735`, `skipped=0`.
- `mas004-rpi-databridge.service` wurde neu installiert/restarted und ist wieder `active`; `/health` antwortet `{"ok":true}`.
- SmartWickler-Firmwares wurden danach erfolgreich geflasht:
  - Abwickler ueber `COM9`
  - Aufwickler ueber `COM8`
- ESP32-PLC-Firmware wurde nach erneut sichtbarem `/dev/esp32plc58` ueber den Raspi erfolgreich geflasht.
- ESP-Verifikation nach Flash:
  - `PING -> PONG`
  - `INFO` meldet `MAS-004_ESP32-PLC-Firmware`, `ip=192.168.2.101`, `port=3010`
  - `PROCESS INDEXED STATUS?` liefert JSON mit `running=false`, `completed=true`, `last_error="reset"`


## 2026-05-18 (Externe Testbefehle entfernt)
- Die temporaere externe Testbefehlsfamilie wurde aus Master-Excel, Router, Tests und Dokumentation entfernt.
- Der produktive Wickler-Einrichtablauf bleibt intern erhalten: Beim Wechsel in den Einrichtbetrieb fuehrt der Raspi die Wickler-Kalibrierung, 1000-mm-Messfahrt und Durchmesseruebernahme selbst aus.
- Externe Testkommandos fuer getaktete/continuous Bewegungen werden nicht mehr angeboten. Produktionsnahe Tests laufen ueber MAS0002, Format-/MAP-Parameter und die ESP32-PLC-Ablaufsteuerung.

## 2026-04-30 (Masterdaten: Wickler-Fuellstandswarnungen getrennt)
- Master-Workbook `Parameterliste SAR41-MAS-004.xlsx` aktualisiert und Repo-Kopie in `master_data/` synchronisiert.
- `MAP0023` bleibt Abwickler-Vorwarnung bei niedrigem Fuellstand: `MAS0008 <= MAP0023`, Default `5 %`.
- `MAP0024` ist jetzt die Aufwickler-Vollwarnung: `MAS0009 >= MAP0024`, Default `95 %`.
- KI-Anweisungen und Sync-Hilfstext wurden korrigiert, damit die alte falsche Aufwickler-Logik `MAS0009 < 5 %` nicht wieder generiert wird.

## 2026-04-22 (Production IBN Cutover)
- Added a dedicated internal HTTP device inbox on `:8081` for ESP32/W5500 devices that cannot post to the HTTPS UI/API port directly.
  - Public/operator UI remains HTTPS on `:8080`.
  - The internal device inbox only exposes `/api/inbox` and `/health`.
  - `/api/inbox` still requires the configured `X-Shared-Secret`.
- Retired the temporary former-TEST Raspi address from active deployment defaults and current docs.
- Active local production/commissioning defaults now use:
  - Raspi/UI/API: `10.141.94.213`
  - engineering laptop / Microtom simulator: `10.141.94.212`
- Verified Microtom simulator HTTPS certificate and listener on `https://10.141.94.212:9090`.
- Fixed the outbound HTTP client source-IP bind for the Raspi runtime: `httpx.HTTPTransport(local_address=...)` now receives the source IP string instead of a `(host, port)` tuple, and custom transports receive the configured TLS verification flag directly. This resolves the source-bind `TypeError` and keeps self-signed Microtom simulator callbacks working when `tls_verify=false`.
- Corrected the Raspi system clock manually during IBN because the cutover network no longer reached the old NTP path.
- Disabled the legacy standalone `mas004-esp32-plc-bridge.service` on the production/commissioning Raspi. The RPI-Databridge is now the sole live owner of the ESP endpoint `192.168.2.101:3010`; keeping both services active can occupy the ESP single-client command socket and disturb direct smoke tests.
- Reissued the production/commissioning Raspi WebUI certificate for `https://10.141.94.213:8080`:
  - the previously active certificate still used `CN/SAN=10.27.67.68`
  - the new self-signed certificate has SAN `IP:10.141.94.213` and is installed in the Windows CurrentUser Root store on the engineering laptop
  - strict Windows/Schannel smoke test `curl https://10.141.94.213:8080/health` returns `{"ok":true}`
- Updated the guided production IBN Wickler phase so SmartWickler flashing is role-specific:
  - Abwickler: `-WicklerRole abwickler` -> SmartWickler env `abwickler`
  - Aufwickler: `-WicklerRole aufwickler` -> SmartWickler env `aufwickler`

## 2026-04-21 (Smart Wickler USB Deploy Decision)
- Confirmed the production IBN decision that the two Smart Wicklers remain autonomous.
- The Raspi remains the USB flash gateway only for the ESP32-PLC58.
- Abwickler and Aufwickler are flashed manually and sequentially on the engineering laptop via `DeployWicklerUsb`.
- Updated the guided production IBN script so the Wickler phase explicitly refuses the Raspi-gateway interpretation and uses the local PlatformIO executable fallback when `pio` is not on PATH.

## 2026-04-21 (Production Stand 10.141.94.x Offline Preparation)
- Prepared the former TEST Raspberry as the next production/commissioning stand without contacting the target.
- Added a `production` target profile for the post-cutover Raspi address `pi@10.141.94.213`; the temporary bootstrap target is now retired.
- Added machine-readable production topology and commissioning config patch files:
  - `scripts/production_topology_10_141_94.json`
  - `scripts/production_commissioning_config_patch_10_141_94.json`
- Added guided IBN helper `scripts/mas004_production_ibn.ps1` with phases for:
  - local precheck
  - Raspi deploy while still on the temporary bootstrap address
  - Databridge runtime config staging
  - explicit OS network cutover to `10.141.94.213/24`
  - post-cutover status check
  - ESP flash via Raspi USB alias
  - Smart Wickler USB flash guidance
- Production eth0 target addresses are now documented as:
  - Laptop/Microtom testtool `10.141.94.212`
  - Raspi `10.141.94.213`
  - TTO `10.141.94.214`
  - Laser `10.141.94.215`
  - Abwickler `10.141.94.216`
  - Aufwickler `10.141.94.217`
- ETH1 remains unchanged with ESP `192.168.2.101` and Moxa `192.168.2.102/103`.
- LIVE and the production Raspi were not contacted; this is an offline preparation for tomorrow's IBN.

## 2026-04-21 (Master Parameter Workbook Refresh Offline)
- Refreshed the external master workbook and repo copy from:
  - `..\Parameterliste SAR41-MAS-004.xlsx`
  - `master_data/Parameterliste SAR41-MAS-004.xlsx`
- No parameter IDs were added or removed in this sync.
- Applied 56 MAP-row changes, mostly around `ESP32 R/W` and `KI-Anweisungen:`.
- Evaluated the operator notes that were written before `KI:` in the workbook and regenerated 62 KI cells as clean `KI:` interpretations.
- Updated `MAP0066` to default `8000` and `ESP32 R/W = R`.
- Added `format_semantics.py` to derive a deterministic Raspi-side format/process plan from the current MAP values.
- Updated the `MA*` routing rule for `ESP32 R/W = R`:
  - Microtom writes are accepted and stored on the Raspi
  - the value is mirrored to ESP with the new firmware-side `SYNC <key>=<value>` command
  - normal ESP write routing remains reserved for `ESP32 R/W = W` / `R/W`
- Label length deviations `MAE0025` and `MAE0026` now pause production on the Raspi instead of being classified as Purge/Not-Stop reasons.
- LIVE and TEST were not contacted; this is an offline local/Git preparation.

## 2026-04-18 (Commissioning Assistant Refinement + LIVE Peer Clarification)
- Refined the protected commissioning assistant so it follows the real MAS-004 hardware bring-up order instead of only broad buckets.
- Added explicit MAS-004-focused commissioning steps for:
  - Microtom primary and optional secondary/VPN peer health
  - ESP realtime IO/process image
  - Moxa field IO validation
  - TTO and Laser IO handshake checks
  - winder stop IO validation
  - grouped axis commissioning for X/Z, label drive, sensor axes, camera axis, laser guard and label guides
  - dedicated encoder, sensor, camera, machine-state and `MAS0030` logfile validation
- Added Raspi-side HTTP health probing for the primary and optional secondary peer checks.
- Reverified the LIVE secondary/VPN peer path after restarting the local Microtom simulator on `https://192.168.5.2:9090`:
  - `/health` answered from LIVE
  - `[OUTBOX:aux]` callbacks returned HTTP `200`
  - observed callback times stayed in the low millisecond range (`37-66 ms`)
- Clarified the current LIVE callback fault boundary:
  - the journal entry `POST http://192.168.210.10:81/api/inbox -> HTTP 404: "No active developers found to forward the request"` is a primary-peer/Microtom-side behavior
  - it is not caused by the optional secondary peer being down

## 2026-04-18 (LIVE Deploy: Commissioning / Backup Workflows)
- Deployed the new protected commissioning/backup feature set to the Microtom LIVE Raspberry at `192.168.210.20`.
- Applied the code via Git patch into `/opt/MAS-004_RPI-Databridge`.
- Rebuilt the installed package inside `/opt/MAS-004_RPI-Databridge/.venv` with:
  - `./.venv/bin/pip install --no-deps .`
- Restarted `mas004-rpi-databridge.service`.
- LIVE post-deploy verification:
  - service returned to `active`
  - `https://127.0.0.1:8080/health` answered with `{"ok":true}`
  - `/ui/machine-setup/commissioning` redirected to the protected login as expected
- The deployment intentionally did not alter the persisted LIVE website/runtime settings.

## 2026-04-18 (Commissioning Assistant + Machine Backup/Clone Documentation)
- Documented the new protected `Machine-Setup` additions:
  - `/ui/machine-setup/commissioning`
  - `/ui/machine-setup/backups`
- Documented the commissioning assistant concept:
  - full run versus incomplete-only rerun
  - recorded step states
  - guided bring-up across Raspi, endpoints, IO, motor and safety validation
- Documented machine identity handling:
  - `machine_serial_number`
  - `machine_name`
  - `backup_root_path`
- Documented the backup strategy:
  - settings backup for machine-local runtime state
  - full backup for clone/disaster-recovery baselines with repo snapshots
  - import/export/restore expectations
- Documented the intended scripted recovery path:
  - `scripts/mas004_machine_bootstrap.py`
  - `scripts/mas004_restore_backup.py`
- Added explicit LIVE/TEST/offline-mode notes so deployment, restore and commissioning remain traceable even while only one Raspberry or no target is reachable.

## 2026-04-17 (Machine Runtime Foundation + Workbook/KI Sync)
- Added a first Raspi-side machine runtime foundation:
  - new modules:
    - `mas004_rpi_databridge/machine_runtime.py`
    - `mas004_rpi_databridge/machine_semantics.py`
  - new persisted runtime tables:
    - `machine_state`
    - `machine_events`
    - `label_register`
    - `label_events`
  - new protected process/operator view:
    - `/ui/machine-setup/process`
    - `/api/machine/overview`
- `MAS0002` is now treated as a Microtom command byte instead of being misread as a direct machine state:
  - command values are translated into the correct target states and transition states on the Raspi side
- Added `LabelProductionLog` as an extra production logfile stream for completed label results / `MAS0003`.
- Added reusable workbook sync automation:
  - `scripts/sync_master_workbooks.py`
  - syncs the current external parameter workbook into the repo copy
  - copies the current IO workbook into `master_data/`
  - inserts `MAP0066`
  - refreshes the full `KI-Anweisungen:` column with `KI:` texts
- Hardened the web app for reduced Python environments:
  - `/api/params/import` and `/api/io/import` now degrade to `503` instead of breaking app startup entirely when `python-multipart` is missing.

## 2026-04-17 (Top Navigation Order Adjusted)
- Reordered the main top navigation buttons to:
  - `Home`
  - `Parameter`
  - `Test UI`
  - `API Docs`
  - `Settings`
  - `Machine-Setup`
- The protected `Machine-Setup` area and its login/session behavior stay unchanged; only the visible button order was adjusted.

## 2026-04-17 (Hardware IO Integration + Network Split Documented)
- Documented the new hardware IO basis for the merged plant topology:
  - `ESP32-PLC58`
  - `Raspberry PLC21`
  - `2x Moxa ioLogik E1211`
- Documented the current network split:
  - `eth0 / 192.168.210.20` for Microtom, VJ6530, VJ3350, Abwickler and Aufwickler
  - `eth1 / 192.168.2.100` for ESP32-PLC58 and the two Moxa modules
- Added the dedicated IO workbook import path to the project context:
  - `master_data/SAR41-MAS-004_SPS_I-Os.xlsx`
- Recorded the new Machine-Setup I/O page and its workbook-driven IO overview/write access.
- Recorded that the Raspi hardware IO layer stays simulation-first by default until an approved Industrial Shields library installation is available on the Raspberry runtime.
- Recorded that the Moxa modules are handled as slow supervisory IO on the Raspi side so the ESP32 remains focused on realtime machine tasks.

## 2026-04-17 (Machine-Setup Protected Menu)
- Reworked the top navigation so `Motors` now lives under a dedicated `Machine-Setup` menu entry.
- Added a dedicated Machine-Setup login flow with cookie-backed session protection for:
  - `/ui/machine-setup/motors`
  - `/ui/machine-setup/winders/unwinder`
  - `/ui/machine-setup/winders/rewinder`
  - `/api/motors/*`
  - `/api/winders/*`
- Fixed credentials for this protected section are now documented in the local support docs:
  - user `Admin`
  - password `VideojetMAS004!`
- Legacy `/ui/motors` and `/ui/winders/*` endpoints now redirect into the protected Machine-Setup area for compatibility.
- Smart Wickler navigation no longer opens a separate browser window; `Abwickler` and `Aufwickler` stay inside the main Raspi UI shell.

## 2026-04-17 (Master Workbook Reimport Sync)
- Refreshed the repository copy `master_data/Parameterliste SAR41-MAS-004.xlsx` from the current external master workbook.
- Workbook delta in this sync:
  - added `MAP0065`
  - updated Microtom `R/W:` for `MAP0056..MAP0064` from `W` to `R`
  - corrected several `MAP0059..MAP0064` display names to use the `MAP` prefix consistently
- LIVE workbook import path is now expected to refresh both the SQLite parameter metadata and `/var/lib/mas004_rpi_databridge/master/Parameterliste_master.xlsx` from the same uploaded workbook.

## 2026-04-16 (Per-Motor Simulation on Motors UI)
- Added per-motor simulation toggles directly on `/ui/motors`.
- Simulated motors are no longer queried through the ESP endpoint during the periodic UI refresh.
- The Raspi now keeps a local motor UI cache and shows last known values or machine defaults while a motor stays in simulation.
- Live-only motor actions are disabled in the UI when a motor is marked as simulated.
- When all motors are currently in simulation, the Motors UI now pauses auto-refresh instead of flipping the status text between `loading...` and the loaded state.
- Motor binding lookup for `/api/motors/overview` is now cached in-process so the initial page load is lighter.

## 2026-04-16 (Motors UI Fallback + Smart Wickler Integration + Logo Restore)
- The Raspi `Motors` page no longer depends on a live ESP motor endpoint to render:
  - all 9 Oriental motor cards are now shown from a fixed machine catalog even when the ESP endpoint is missing or still in simulation
  - live ESP motor data overlays onto that catalog only when reachable
- Added Raspi-side Smart Wickler integration:
  - new device endpoint settings for `Abwickler` and `Aufwickler`
  - each endpoint has `host`, `port` and `simulation`
  - recommended sequential defaults:
    - `Abwickler` -> `192.168.2.104:3011`
    - `Aufwickler` -> `192.168.2.105:3012`
- Added new Raspi UI proxy pages:
  - `/ui/winders/unwinder`
  - `/ui/winders/rewinder`
  - this original standalone navigation was later superseded by the protected `Machine-Setup` shell
- The Raspi Wickler pages read `/api/state` from the configured Smart Wickler endpoint when live is enabled and fall back to a stable local simulation/offline visualization otherwise.
- Restored robust Videojet logo delivery:
  - `videojet-logo.jpg` is now included as package data for installed Raspi builds
  - the web UI additionally falls back to the repo asset path on the Raspberry if needed

## 2026-04-16 (LIVE Deployed + Secondary VPN Callback Reverified)
- Deployed the merged local Databridge mainline to the Microtom LIVE Raspberry:
  - LIVE repo `/opt/MAS-004_RPI-Databridge` is now on `f660b69`
  - runtime package was reinstalled and `mas004-rpi-databridge.service` restarted without changing the LIVE UI/config settings
- Also aligned the currently reachable LIVE companion repos to the local merged basis:
  - `MAS-004_VJ6530-ZBC-Bridge` -> `09f9397`
  - `MAS-004_ZBC-Library` -> `c47563d`
  - `MAS-004_ESP32-PLC-Firmware` -> `61e9ef0`
- Reverified the secondary VPN callback path on LIVE against `peer_base_url_secondary = https://192.168.5.2:9090`:
  - five consecutive `MAS0030=?` requests reached the secondary peer in about `31 ms`, `31 ms`, `35 ms`, `37 ms`, `54 ms`
  - at the same time the primary peer `http://192.168.210.10:81/api/inbox` still timed out with the expected `~10 s` `ReadTimeout`, but no longer blocked the secondary lane
- Corrected the LIVE workbook/DB access metadata for `MAS0029`:
  - `esp_rw` changed from stale `R` to workbook-correct `N`
  - the live value itself stayed untouched (`default_v = 987654` at time of correction)

## 2026-04-16 (Outbox Lane Split for Slow Microtom Inbox Callbacks)
- Diagnosed the LIVE Microtom callback delay pattern on `192.168.210.20`:
  - the Databridge sender was single-threaded
  - `http_timeout_s = 10.0`
  - repeated `ReadTimeout('timed out')` on `http://192.168.210.10:81/api/inbox` produced the observed `10s / 20s / 30s ...` stagger
- Added URL-filtered outbox selection so sender lanes can work independently per target bucket.
- Split the sender runtime into:
  - `primary` lane for `peer_base_url` with the existing watchdog/retry behavior
  - `aux` lane for all non-primary targets, including `peer_base_url_secondary`
- Result:
  - a slow or timing-out primary Microtom inbox no longer blocks secondary or custom callback targets
  - primary retries remain intact
  - secondary still drops on failure as before
- Added regression coverage for:
  - filtered `Outbox.next_due(...)` selection
  - sender lane topology with and without a configured primary peer

## 2026-04-16 (Oriental Motor Setup Layer Added Offline)
- Switched the repository-default master workbook copy to `master_data/Parameterliste SAR41-MAS-004.xlsx`.
- Extended parameter import/export so the workbook column `KI-Anweisungen:` is stored as `ai_instructions`.
- Added workbook-driven motor binding derivation from `KI-Anweisungen:` for the Oriental motor set:
  - `MAP0056..MAP0064` as Sollwerte
  - `MAS0011`, `MAS0012`, `MAS0013`, `MAS0014`, `MAS0015`, `MAS0016`, `MAS0017`, `MAS0031`, `MAS0032` as Istwerte
  - `MAE0004..MAE0010`, `MAE0046`, `MAE0047` as controller fault mirrors
- Added a new Raspi operator tab `/ui/motors` plus matching `/api/motors/*` endpoints for:
  - live overview of the 9 Oriental motors behind the ESP32-PLC
  - manual move in steps/mm
  - zero/min/max capture
  - editable motion defaults (`steps/mm`, speed, current, acceleration, deceleration, soft limits, direction)
- The new motor UI explicitly protects focused/dirty inputs from being overwritten by the refresh loop.

## 2026-04-16 (Offline Coordination Mode Reconfirmed)
- Reconfirmed the canonical MAS-004 sub-agent roster for the master chat:
  - `mas004_docs`
  - `mas004_rpi_core`
  - `mas004_param_master`
  - `mas004_esp32_bridge`
  - `mas004_esp32_firmware`
  - `mas004_smartwickler`
  - `mas004_vj3350_bridge`
  - `mas004_vj6530_bridge`
  - `mas004_zbc_library`
  - `mas004_release_ops`
- Recorded that current workshop work continues in offline mode because TEST, LIVE, Microtom peers and field devices are all unreachable from this workstation.
- Captured the current local repo/Git snapshot in `PROJECT_CONTEXT.md`.
- TEST/LIVE synchronization remains intentionally open and must be revisited once connectivity returns.

## 2026-04-16 (LIVE SSH Access Standardized)
- Standardized LIVE Raspberry SSH access on this laptop:
  - direct `ssh pi@192.168.210.20` now uses the dedicated MAS-004 key automatically
  - alias `mas004-rpi-live` added alongside existing `mas004-rpi`
- Confirmed the working LIVE key path:
  - `C:/Users/Egli_Erwin/.ssh/mas004_rpi210_ed25519`
- Documented the current LIVE fallback password and the preferred key-based path in `PROJECT_CONTEXT.md` and `SUPPORT_RUNBOOK.md`.

## 2026-04-09 (SmartWickler Subproject Added)
- Added the new subproject `MAS-004_SmartWickler` to the MAS-004 orchestration landscape.
- The canonical new owner role is `mas004_smartwickler`.
- The preferred architecture for the wicklers is now documented as:
  - local real-time control loop on the SmartWickler ESP32-S3
  - direct Ethernet/API coupling to `MAS-004_RPI-Databridge`
  - no real-time winding-control detour through `MAS-004_ESP32-PLC`

## 2026-03-26 (ESP Firmware TTO Mirror Gap Closed)
- The remaining TEST ESP gap for mirrored 6530 state rows is closed:
  - `TTS0001`, `TTP00073`, `TTP00076` no longer fail with `NAK_UnknownParam` on the real ESP
- Root cause was on the ESP firmware side, not in the Raspi async path:
  - the ESP seed generator only covered `MAP` / `MAS` / `MAE` / `MAW`
  - the real device therefore had no seeded slots for the mirrored 6530 rows
- TEST proof after the firmware refresh:
  - direct smoke on `192.168.2.101:3010` confirmed `ACK_TTS0001=3`, `ACK_TTP00073=1`, `ACK_TTP00076=ONLINE`
- The async Raspi fanout logic stays unchanged; this entry closes the documented ESP-side limitation from the previous TEST runs.

## 2026-03-25 (6530 Async Proof + Background ESP Mirror)
- Live raw ZBC verification on TEST now confirms that `AIS/AIR` is really active and immediate on the 6530:
  - `CMD_START` produced `AIR` tag `0x0002` in about `46 ms`
  - `CMD_STOP` produced `AIR` tag `0x0008` in about `6 ms`
- The Databridge async owner now mirrors 6530-originated values to the ESP on a dedicated background worker instead of on the async listener thread itself.
- Result:
  - Microtom fanout no longer waits behind slow ESP writes
  - transient ESP communication failures retry in the worker
  - permanent ESP rejections such as `NAK_UnknownParam` remain visible in the logs instead of silently disappearing
- Historical TEST finding before the 2026-03-26 firmware update:
  - `TTP00073`, `TTP00076`, `TTS0001` were rejected by the real ESP with `NAK_UnknownParam`, even though the Microtom path itself was healthy.

## 2026-03-25 (6530 `6 -> 3` Confirmation Uses Live Summary)
- Fixed the remaining `TTS0001=3` failure from `SHUTDOWN (6)`: the Raspi no longer trusts a stale shutdown snapshot while confirming the `STARTUP` step.
- The upper 6530 write path now benefits from the shared-library fix that reads fresh summary state for `STARTUP` / `SHUTDOWN`, because those transitions do not emit their own dedicated `AIR` state tag on the TEST printer.
- Result on TEST:
  - `TTS0001=6` settles to `SHUTDOWN` in about `4.1 s`
  - `TTS0001=3` from `6` now reaches `ONLINE` in about `3.2 s` instead of ending in `NAK_DeviceComm`

## 2026-03-25 (6530 ACK Follows Async-Observed State)
- Runtime-session `STATUS[PRINTER_STATE_CODE]` writes no longer trust a stale synchronous verify value if the async owner session has already observed a newer real printer state.
- The Databridge now waits on the workbook-backed async state update for `TTS0001` and acknowledges the settled observed printer state (`0/1/2`, `3/4/5`, `6`) instead of echoing an outdated `0`.
- Result: `TTS0001=3` no longer returns `ACK_TTS0001=0` simply because the direct verify path lagged behind the AIR-driven state transition.
- Added regression coverage for runtime-session status writes where the direct verify value is stale but the async-observed state already reached the requested target.

## 2026-03-25 (6530 AIS Priority + Immediate Snapshot Push)
- The 6530 async owner now subscribes to online/offline/warning/fault plus print-failed AIS events with the high-priority flag, matching the real-time requirement from the ZBC spec for critical state changes.
- Incoming AIR tag changes now update workbook-backed `STATUS[...]` / `STS[...]` rows immediately from the async snapshot before the slower summary settle runs.
- Result: `TTP00073`, `TTP00076`, `TTS0001` and similar state rows no longer have to wait for the summary reread before they can be forwarded to Microtom / ESP.
- Session-owner requests now temporarily raise the live ZBC response-time budget per operation, so slow `CMD_STARTUP` / `CMD_START` transitions no longer fail early with `NAK_DeviceComm` just because the listener keeps a short unsolicited receive timeout.
- The fallback poller now also stands down while the async owner session is healthy, not only after a fresh async event, reducing stale or delayed poll-derived state updates.
- Added regression coverage for:
  - immediate snapshot-driven state fanout from async events
  - write requests using the longer owner-session response timeout
  - poller stand-down while the async owner is healthy

## 2026-03-25 (6530 Immediate ACK + Non-Blocking Event Fanout)
- The queued 6530 owner-session write path now returns success to the caller as soon as the live write itself succeeds; the post-write summary settle still runs, but no longer causes false `NAK_DeviceComm` on slow state transitions such as `TTS0001=3`.
- Runtime-session 6530 writes now wait longer before timing out, matching the observed `STARTUP -> START` transition time on the real TEST printer.
- The owner-session timeout budget for queued 6530 writes is now deliberately larger than the shared-library settle window, so the Databridge does not abort slow `SHUTDOWN -> ONLINE` transitions before the library has finished waiting for the real target state.
- 6530 async summary updates now enqueue all Microtom notifications before any ESP mirror attempt starts, so slow or failing ESP mirrors no longer delay `TTP00073` / `TTP00076` / `TTS0001` delivery to Microtom.
- The async keepalive cadence was tightened from ~8s to ~5s to give more headroom before the printer closes an idle TCP AIS session.
- The fallback poller now stands down for a short grace window after a fresh async event and discards any overlapping poll result if async state arrived first.
- Background 6530 cache warmup is now skipped entirely while async ownership is enabled, removing another avoidable second-client collision on `3002`.
- Added regression coverage for:
  - queued async-session writes returning before the post-write summary settle
  - Microtom event enqueue staying complete even when the ESP mirror path is slow or failing
  - stale fallback poll results no longer overriding fresh async state

## 2026-03-25 (6530 Single-Owner Session + AIS Keepalive)
- Live TEST verification against `192.168.2.103:3002` showed:
  - `AIS` without synchronous traffic is closed by the printer after roughly 15s
  - `AIS` stays stable when the host sends `IRQ([])` keepalives about every 8-10s
  - a second synchronous control session times out while the async subscription is already holding the live `3002` owner slot
- The Databridge now treats the 6530 path as a single-owner session:
  - the async listener negotiates host version (`HCV`) on session start
  - the async listener keeps the subscription alive with `IRQ([])` instead of relying on idle receives
  - synchronous 6530 mapping reads/writes are now routed through the already-open async owner session when that session is active
- Result: status pushes can stay immediate while Microtom/ESP writes no longer have to fight a second parallel ZBC connection on `3002`.
- The forced 30s async session rotation was removed; reconnects now happen on real socket/protocol errors instead of a deliberate timer.
- Added regression coverage for:
  - runtime request hand-off into the 6530 owner session
  - DeviceBridge writes using the runtime session instead of opening a second bridge client
  - Poller reads using the runtime session when async ownership is active

## 2026-03-25 (6530 Lossless State Forwarding + Async Stabilization)
- The 6530 async listener now prefers the already verified `vj6530-tcp-no-crc` transport profile instead of re-probing every async session startup; autodetect remains fallback only.
- The async loop now treats idle `socket.timeout` on the unsolicited receive path as a healthy wait state instead of tearing the subscription down as an error.
- Async status refreshes now use a short summary settle window after online/offline/warning/fault events so follow-up state transitions like `OFFLINE -> SHUTDOWN` or `OFFLINE -> ONLINE` are more likely to be captured immediately.
- The 6530 fallback poller now stands down while the async channel is still healthy, but the async-health age-out window was reduced so fallback reconciliation resumes much sooner after a broken async session.
- The background 6530 poll loop now reuses one bridge client while host/port/timeout stay unchanged, so profile knowledge is no longer thrown away on every cycle.
- Successful 6530 writes from Microtom or ESP now trigger an immediate workbook status resync so related status rows such as `TTP00073`, `TTP00076`, `TTS0001`, `TTE*`, `TTW*` do not wait for the next background cycle.
- The async loop now proactively rotates 6530 subscriptions every 30s and reconnects near-immediately after printer-driven `socket closed` events, reducing blind windows between state pushes.
- The async listener now marks the channel healthy immediately after a successful subscription, so the fallback poller does not race the first startup summary refresh.
- The service now starts the 6530 async thread before the fallback poller and gives it a short head start during boot, removing another startup race on `3002`.
- The multi-repo sync script now skips restarting services that are explicitly `disabled` or `masked` on the target, so intentionally parked side daemons do not reappear during routine sync.
- Outbox dedupe is now lossless for non-consecutive state changes:
  - consecutive duplicate values may still collapse
  - alternating sequences like `3 -> 0 -> 3` are preserved as separate queued deliveries

## 2026-03-25 (6530 Event Rights + `TTS0001` Status Channel)
- Added protocol/runtime support for `TTS0001` as the dedicated numeric TTO status parameter (`STATUS[PRINTER_STATE_CODE]`).
- Numeric state mapping is now:
  - `0=OFFLINE`
  - `1=OFFLINE_WARNING`
  - `2=OFFLINE_FAULT`
  - `3=ONLINE`
  - `4=ONLINE_WARNING`
  - `5=ONLINE_FAULT`
  - `6=SHUTDOWN`
- ESP writes to `TTS0001` now drive the printer through the existing 6530 control path for the directly commandable states `0`, `3`, `6`.
- Live refinement on TEST:
  - `TTS0001=3` from `SHUTDOWN (6)` now executes as `STARTUP` then `START`
  - `TTS0001=0` from `SHUTDOWN (6)` now executes as `STARTUP`
  - the derived state targets `1`, `2`, `4`, `5` are now rejected cleanly instead of surfacing as misleading `NAK_DeviceComm`
- The 6530 async listener and fallback poller now respect the workbook access flags before forwarding printer-originated updates to Microtom.
- The fallback poller now also keeps workbook status/error mappings from `TTP` / `TTS` in sync instead of only `TTE` / `TTW`.
- Added regression coverage for:
  - `TTS0001` protocol normalization and ESP write handling
  - active async push of `TTE` and `TTS`
  - poller-side `TTS` updates with Microtom access denied

## 2026-03-25 (Retry Once Before `NAK_DeviceComm` on VJ6530)
- Current-parameter reads and writes on the 6530 live path now retry once before falling back to cached values or bubbling up to the generic `NAK_DeviceComm`.
- Goal: absorb transient profile-detect / session timeouts on `3002` that would otherwise fail a user write even though the immediate retry succeeds.
- Added regression tests for:
  - a flaky current-parameter read that succeeds on the second attempt
  - a flaky current-parameter write that succeeds on the second attempt

## 2026-03-25 (Respect `esp_rw = N` for MA* Live Routing)
- Fixed the router so `MAP` / `MAS` / `MAE` / `MAW` parameters with `esp_rw = N` stay Raspi-local even when ESP live mode is enabled.
- This closes the mismatch where the simulation path accepted local-only parameters such as `MAS0029`, but the real ESP path still forwarded them and collapsed the device-side rejection into `NAK_DeviceRejected`.
- Added a regression test for:
  - local-only `MAS0029` with `esp_rw = N`
  - ESP-routed `MAS0026` with `esp_rw = W`

## 2026-03-25 (Canonical Sub-Agent Rehydration Policy)
- Clarified that the MAS-004 sub-agents are canonical long-lived project roles even if individual agent threads disappear from the UI.
- Documented the required fallback for platform slot/session limits:
  - re-create missing agents under the exact same canonical names before delegating further work
  - keep ownership boundaries unchanged
  - report which named agents are live versus temporarily parked behind the current slot limit

## 2026-03-25 (Master Chat / Sub-Agent Orchestration Blueprint)
- Added `docs/MAS-004_Roche_Master_Chat.md` as the recommended bootstrap instruction for the future master chat `MAS-004_Roche`.
- Defined a stable long-term sub-agent topology with dedicated owners for:
  - documentation
  - main Databridge core
  - parameter master data / Excel mappings
  - ESP32 bridge
  - ESP32 firmware
  - VJ3350 bridge
  - VJ6530 bridge
  - ZBC shared protocol library
  - release / deployment operations
- Documented coordination rules so future multi-repo work can scale without overlapping file ownership or mixing protocol, business and deployment responsibilities.

## 2026-03-25 (Local Timezone for Log UI and Logfiles)
- Added `mas004_rpi_databridge/timeutil.py` as the central source for local system timezone resolution.
- Daily logfiles, production logfiles and DB-backed log downloads now format timestamps via the current Raspi timezone instead of relying on implicit process-local UTC behavior.
- Test UI log windows now use server-provided `ts_display` values instead of browser-side `toISOString()` formatting.
- Goal: all log windows and logfile exports follow the synchronized Raspi local time consistently.

## 2026-03-25 (Settings UI: System Time / Timezone / NTP Status)
- Added token-protected endpoint `GET /api/system/time`.
- `ui/settings` now shows:
  - current local system time
  - current system timezone
  - synchronized yes/no state
  - OS NTP service state
  - detected OS time source
- This status is read-only and complements the existing Databridge-side `ntp_server` / `ntp_sync_interval_min` settings.
- Clarified operational expectation: the Microtom LIVE Raspberry remains in timezone `Europe/Zurich`.

## 2026-03-25 (Production Logfiles via MAS0002 / MAS0029 / MAS0030)
- Added `mas004_rpi_databridge/production_logs.py`.
- Production log capture is now controlled by MAS status values:
  - `MAS0002=1` starts a production log session
  - `MAS0002=2` stops the session and marks the last production logs as ready
- Added new workbook parameters:
  - `MAS0029` production label / logfile suffix (string)
  - `MAS0030` production-logfiles-ready flag (`0|1`)
- `logstore.py` now mirrors active communication into separate production TXT files:
  - `gesamtanlage_<MAS0029>.txt`
  - `esp32_plc_<MAS0029>.txt`
  - `tto_6530_<MAS0029>.txt`
  - `laser_3350_<MAS0029>.txt`
- Added Microtom pull endpoints:
  - `GET /api/production/logfiles/list`
  - `GET /api/production/logfiles/download`
  - `POST /api/production/logfiles/ack`
- When a production stops, the Raspi now raises `MAS0030=1` so Microtom can detect that the last production logs are ready to fetch.
- Production-log downloads are now consumptive:
  - downloading a production TXT file removes it from the Raspi immediately
  - after the final production file is downloaded, the Raspi automatically sets `MAS0030=0`
  - that reset is also forwarded automatically to Microtom via callback `/api/inbox`
- A new production cannot be started with `MAS0002=1` while old production files are still pending:
  - the Raspi now returns `MAS0002=NAK_ProductionLogfilesPending`
- Daily and production TXT logfiles are now enriched with workbook metadata:
  - parameter `Name`
  - parameter `Message` / description text

## 2026-03-25 (LIVE/Test State Merge for Microtom Rollout)
- Reconciled the code baseline between the TEST branch work and the current Microtom LIVE system.
- Confirmed that LIVE runtime settings remain external in `/etc/mas004_rpi_databridge/config.json` and are not touched by repo deployment.
- Captured the current LIVE runtime snapshot in `docs/PROJECT_CONTEXT.md` so the same values can later be mirrored to TEST when the TEST Raspberry is reachable again.
- Prepared LIVE deployment to bring these repo states in line:
  - `MAS-004_RPI-Databridge`
  - `MAS-004_ESP32-PLC-Bridge`
  - `MAS-004_VJ3350-Ultimate-Bridge`
  - `MAS-004_VJ6530-ZBC-Bridge`
  - `MAS-004_ZBC-Library`

## 2026-03-17 (Former TEST IP Change)
- Changed the TEST target away from the obsolete `.69` address to the temporary bootstrap address used at that time.
- Updated deployment target metadata, project context and runbook documentation.
- TEST Raspi network and HTTPS endpoint are now expected at:
  - SSH/UI/API used the temporary bootstrap address that is now retired

## 2026-03-13 (6530 Async Primary + Versioned Master Workbook)
- Added `mas004_rpi_databridge/vj6530_runtime.py` to track whether the 6530 async channel is currently healthy.
- Refactored `mas004_rpi_databridge/vj6530_async_listener.py`:
  - async subscription now keeps one live ZBC session and resolves `STATUS[...]`, `TTE`, `TTW` updates from the same summary channel
  - this avoids the previous second-connection timeout pattern against the real printer
- `mas004_rpi_databridge/service.py` now treats polling as fallback only:
  - if async is healthy, periodic `TTE` / `TTW` polling is skipped
  - polling resumes automatically if async ages out or fails
- `mas004_rpi_databridge/device_bridge.py` now serves `STATUS[...]` and `IRQ{...}` reads from the Raspi-cached state instead of forcing a live printer roundtrip for every Microtom read.
- `FRQ[CURRENT_PARAMETERS]` reads now fall back to the Raspi-cached TTO value if the live 6530 archive read stalls or times out.
- 6530 async retries now back off progressively instead of hammering the printer every 2s during unstable third-party access on other ports.
- Added background 6530 cache warmup at router startup so the first `TTP` access after a restart is usually already primed.
- Added a repo-tracked master workbook copy:
  - `master_data/Parameterliste SAR41-MAS-004_V11.11.25.xlsx`
  - this is the live-updated workbook with `ESP32 R/W:` and current TTO defaults
- `/api/params/import` now persists the uploaded workbook as Raspi-side master copy at `/var/lib/mas004_rpi_databridge/master/Parameterliste_master.xlsx`.

## 2026-03-13 (6530 Polling + Startup Crash Fix)
- Added `mas004_rpi_databridge/vj6530_poller.py`.
- The Raspi now polls all workbook-mapped `TTE` / `TTW` states from the real 6530 by reusing one summary read per cycle.
- Only changed fault/warning states are persisted locally and forwarded to Microtom.
- Added `vj6530_poll_interval_s` to config, defaults and Settings UI.
- Fixed an installed-package startup regression:
  - `_vj6530_bridge.py` now discovers sibling repos robustly even when the main package runs from `site-packages`
  - this fixes the crash that caused the TEST `/api/inbox` endpoint to refuse connections

## 2026-03-13 (ZBC Library Integration)
- Added `MAS-004_ZBC-Library` as a new managed subproject.
- Extended multi-repo status/sync scripts to include the ZBC library.
- Added bundle-based Pi synchronization for repos without a central Git remote.
- `MAS-004_VJ6530-ZBC-Bridge` has been switched to consume the shared ZBC library.
- Live writeback against the real 6530 is now proven through `FTX[CURRENT_PARAMETERS]`.

## 2026-03-13 (TTO Mapping Routed Live Through ZBC)
- Main-project Excel import now reads the `ZBC Mapping:` column into `param_device_map.zbc_mapping`.
- Database schema migrates existing installations automatically by adding `zbc_mapping` if missing.
- `DeviceBridge` now prefers workbook-based ZBC mappings for `TTP`, `TTE`, `TTW`:
  - `FRQ[CURRENT_PARAMETERS]/...` -> live read/write via the 6530 current-parameter archive
  - `IRQ{LEI,ERR}/Fault[...]` -> live TTE state read
  - `IRQ{LEI,ERR}/Warning[...]` -> live TTW state read
- Live reads from devices now optionally promote the local default value, so the Raspi DB tracks the real device state instead of a stale spreadsheet default.
- The workbook updater now writes current TTP values from the real printer into `Default Value:`.

## 2026-03-13 (TTO Workbook Mapping via Live CurrentParameters)
- Extended `MAS-004_ZBC-Library` with a CLARiTY parameter-archive parser and a `request_current_parameters()` helper.
- Verified live against the 6530 that `FRQ[CURRENT_PARAMETERS]` returns `CurrentParameters.xml`.
- Updated `..\Parameterliste SAR41-MAS-004_V11.11.25.xlsx`:
  - added a `ZBC Mapping:` column
  - repaired `TTP00055` as `TextCommsAsyncNotificationsEnabled`
  - added new TTO parameters `TTP00064` .. `TTP00072`
- Added the reusable workbook updater:
  - `..\MAS-004_ZBC-Library\tools\update_tto_workbook.py`
- Fixed a PowerShell parsing bug in `scripts/mas004_multirepo_sync.ps1`:
  - bundle-sync `scp` target now uses `${resolvedSshHost}` correctly
  - without this, TEST sync for repos without a central remote aborted before transfer
- Fixed bundle-sync behavior for missing remote paths:
  - `MAS-004_ZBC-Library` can now be created on the Pi from the local bundle when `/opt/MAS-004_ZBC-Library` does not exist yet
- Fixed bundle-sync permissions for first deploy:
  - new repos are cloned in `/tmp` and then moved into `/opt/...` via `sudo`
  - this avoids `Permission denied` when the target folder does not yet exist

## 2026-03-13 (Configurable Forwarding Ports + ESP Port 3010)
- TCP forwarding no longer hardcodes device main ports.
  - listeners now follow the configured device ports: `esp_port`, `vj3350_port`, `vj6530_port`
  - this fixes the mismatch where the UI showed `ESP = 3010` but the runtime still listened on `3009`
- Hardened `mas004_rpi_databridge/tcp_forwarder.py` for parallel traffic:
  - shorter upstream connect timeout
  - larger socket buffers
  - `TCP_NODELAY` / keepalive
  - bidirectional pump threads per connection instead of one shared select/send loop
  - active connection tracking and cleaner shutdown on reconcile/restart
- Updated Settings UI text to describe configured main ports plus extra routed ports.
- Fixed ESP line-response parsing in `mas004_rpi_databridge/device_clients.py`:
  - only the first received line is now treated as the response payload
  - prevents heartbeat or extra trailing lines from corrupting `MAP`/`MAS` reads
- Fixed a forwarding regression in `mas004_rpi_databridge/tcp_forwarder.py`:
  - listener sockets no longer get a read timeout
  - this keeps the accept loop alive and fixes hanging routed ports on the TEST target
- Added active ESP push ingestion on the Raspi:
  - new listener `mas004_rpi_databridge/esp_push_listener.py`
  - binds on `eth1_ip:esp_port` when `esp_simulation=false`
  - accepts device-origin `MA*` lines, persists them locally and forwards them to Microtom via outbox
- Moved operation-line parsing into `mas004_rpi_databridge/protocol.py` so router and ESP-push path use the same syntax rules.

## 2026-03-04
- Added persistent support context files:
  - `docs/PROJECT_CONTEXT.md`
  - `docs/SUPPORT_RUNBOOK.md`
  - `docs/SUPPORT_CHANGELOG.md`
- Added multi-repo operations scripts:
  - `scripts/mas004_multirepo_status.ps1`
  - `scripts/mas004_multirepo_sync.ps1`
- Established policy: this repository is the main/orchestration project.
- Baseline local HEAD during this entry: `af82b02`.
- Added current sync snapshot:
  - Main repo synced on Pi.
  - Three Pi subproject repos still dirty and behind 1 (safe-skip mode).

## 2026-03-04 (Pi Safe Cleanup + Full Sync)
- Performed safe cleanup on Pi for all 3 subprojects:
  - Created backup branches and committed tracked local changes before sync.
  - Fast-forwarded `main` to `origin/main`.
- Backup branches created on Pi:
  - `MAS-004_ESP32-PLC-Bridge`: `backup/pi-pre-sync-mas-004_esp32-plc-bridge-20260304-083407`
  - `MAS-004_VJ3350-Ultimate-Bridge`: `backup/pi-pre-sync-mas-004_vj3350-ultimate-bridge-20260304-083551`
  - `MAS-004_VJ6530-ZBC-Bridge`: `backup/pi-pre-sync-mas-004_vj6530-zbc-bridge-20260304-083601`
- Added local Git excludes on Pi subprojects for runtime artifacts:
  - `.venv/`, `*.egg-info/`, `__pycache__/`, `**/__pycache__/`
- Result:
  - Local + Pi + Git are now fully synchronized for all 4 repositories.
  - All 4 systemd services on Pi are active.

## 2026-03-04 (Parallel Microtom Target)
- Added optional secondary Microtom target in config:
  - `peer_base_url_secondary`
- Extended outbound enqueue/routing to fan out to both configured targets:
  - primary `peer_base_url`
  - optional `peer_base_url_secondary`
- Added sender behavior for secondary target as best-effort:
  - failed sends to secondary are dropped (no retry backlog), to protect primary channel latency.
- Updated Settings UI (`/ui/settings`) to edit secondary peer URL.
- Updated default/example config files and project context docs.

## 2026-03-04 (TEST/LIVE Deployment Profiles)
- Added deployment target profile helper:
  - `scripts/mas004_deploy_targets.ps1`
- Updated multi-repo scripts to support `-Target test|live`:
  - `scripts/mas004_multirepo_status.ps1`
  - `scripts/mas004_multirepo_sync.ps1`
- TEST temporary bootstrap address was made the default target for status/sync at that time.
- LIVE (`192.168.1.20`) is blocked by default and requires:
  - `-Target live -AllowLive`
- Added unreachable-target handling so TEST sync can run safely even while test device is not connected.
- Added optional environment variable overrides for host/web target metadata.

## 2026-03-04 (NTP + TCP Relay for Device Ports)
- Added NTP configuration and runtime sync loop:
  - config keys: `ntp_server`, `ntp_sync_interval_min`
  - runtime worker: `mas004_rpi_databridge/ntp_sync.py`
  - Settings UI fields and config API mapping updated.
- Added TCP relay service from Raspi `eth0` to device hosts on `eth1`:
  - initial relay ports: `3007` (VJ6530), `3008` (VJ3350), `3009` (ESP32)
  - optional extra relay ports per device: `esp_forward_ports`, `vj3350_forward_ports`, `vj6530_forward_ports`
  - runtime worker: `mas004_rpi_databridge/tcp_forwarder.py`
  - started from `service.py` at app startup.
- NTP robustness fix:
  - `ntp_sync.py` now searches binaries also in `/usr/sbin`/`/sbin`, so `ntpdate` is detected in systemd service context.

## 2026-03-04 (TEST Raspi Setup Finalization + Deploy Hardening)
- Finalized TEST Raspi setup on the temporary bootstrap subnet:
  - `eth0`: temporary TEST `/24`, gateway `10.27.67.1`, DNS `10.28.193.4 10.27.30.201`
  - `eth1`: `192.168.2.100/24` without gateway
  - timezone set to `Europe/Zurich`
- Root-cause fixed for "new code not active after pull/install":
  - stale `build/` artifacts on Pi caused old package content to be reinstalled.
  - verified by missing `/api/ui/status/public` and absent `[NTP]/[FWD]` runtime logs.
- Deployment hardening:
  - `scripts/mas004_multirepo_sync.ps1` now performs on remote (when `-RestartServices` is used):
    - `rm -rf build`
    - `.venv/bin/python -m pip install --no-cache-dir --force-reinstall .`
    - service restart
- Verified runtime after reinstall:
  - endpoint `GET /api/ui/status/public` returns `200`
  - forwarding listeners active for the configured device ports
  - NTP sync successful against `10.27.30.201`

## 2026-03-12 (Boot Robustness Fixes)
- Fixed TEST/LIVE runtime behavior after reboot:
  - TCP forwarders now reconcile every 5 seconds and retry binds after `eth0` becomes available.
  - This prevents the boot race where `mas004-rpi-databridge` started before `eth0` had carrier and forwarding listeners stayed down.
- Improved NTP sync behavior:
  - command detection now uses explicit executable checks
  - failed sync attempts now report the real command error instead of the misleading "No supported NTP client found"
  - after a failed sync, retry happens after 15 seconds instead of waiting the full configured interval
- Hardened Pi package reinstall in `scripts/mas004_multirepo_sync.ps1`:
  - uses `--no-deps --no-build-isolation`
  - avoids dependency downloads during deploy, which is important when the Pi clock is wrong before first NTP sync

## 2026-04-30 (Temporary Test Commands + Smart Wickler Device Push)
- Added temporary Microtom-test command handling for the process bring-up path:
  - der interne Setup-Wicklerworkflow runs Wickler calibration plus a 1000 mm forward/reverse diameter measurement.
  - die frueheren Bewegungs-Testbefehle starts indexed, continuous forward, or continuous reverse test motion via the ESP32-PLC.
- Hardened the der interne Setup-Wicklerworkflow learning run:
  - the Raspi now waits for the expected Motor-3 travel time and position feedback before starting the reverse pass.
  - this prevents a premature reverse pass if the ESP busy flag is not visible immediately.
  - the measuring pass now uses the ESP command `MOTOR 3 MOVE_REL_MM_OP=...` so forward and reverse setup moves are executed through AZD operation start, not through the production hardware START hold-time path.
- Fixed Smart Wickler status pushes into `/api/inbox`:
  - `source=smartwickler` messages are now treated as device-originated values.
  - read-only Microtom values such as `MAS0008`, `MAS0009`, `MAS0026`, `MAS0027`, and `MAE*` are stored locally and forwarded as status updates instead of returning `NAK_ReadOnly`.

## 2026-04-30 (Raspberry PLC21 IO Runtime Fallback)
- Added a project-local `rpiplc_compat` fallback for Raspberry PLC21 IO access.
- The Databridge still prefers the official Industrial Shields `rpiplc_lib` module when installed.
- If `rpiplc_lib` is missing, the fallback uses the already installed `/usr/lib/librpiplc.so` and the verified `RPIPLC_21` pin mapping for `I0.0..I0.12`, `Q0.0..Q0.7`, and `A0.5..A0.7`.
- This fixes the Machine-Setup IO page showing all Raspberry PLC21 points as offline with `No module named 'rpiplc_lib'` on systems where the native library exists but the Python binding was not installed.

## 2026-05-01 (ESP32-PLC eth1 Communication Hardening)
- Hardened the Raspi-side ESP32-PLC TCP client:
  - per-endpoint lock remains the single production access lane to `192.168.2.101:3010`
  - connection reuse is now semi-persistent and deliberately recycled after 40 request/response cycles
  - a small inter-command pace prevents burst storms against the W5500 socket
  - transient W5500 reconnect windows are retried inside the same command call instead of immediately surfacing as lost commands
- Added `scripts/esp_eth1_stress.py` as a non-invasive stress tool.
- Verified on the production/test machine with Databridge running:
  - `locked-serial`: `1500/1500` safe diagnostic commands acknowledged
  - `locked-parallel`: `1600/1600` commands acknowledged through 8 contending worker threads
  - `busy-probe`: incomplete half-line client returns `NAK_Busy`; after line timeout the endpoint recovers to `PONG`
- Raw unpaced socket storms are not the production contract; production traffic must go through `EspPlcClient` so endpoint locking, pacing and retries are active.

## 2026-05-01 (Machine-Setup IO Persistent Overrides)
- Reworked `/ui/machine-setup/io` output control from momentary `Set 1` / `Set 0` writes to persistent manual overrides:
  - `High` stores a manual high override and is highlighted light green while active.
  - `Low` stores a manual low override and is highlighted light red while active.
  - `Release` clears the override and is highlighted light yellow whenever either High or Low is active.
- Normal machine/runtime writers now respect active IO overrides and do not overwrite the physical output until the override is released.
- IO refresh keeps overridden values visible with quality `override` instead of replacing them with live/simulation snapshots.
- LED commissioning update: the WS2812/FastLED timing is moved to an external ESP32 LED controller. The ESP32-PLC publishes `MAS004-LED-UDP/v1` frames for the shortened `520 mm` / `75` LED strip; TX1/GPIO17 and PLC GPIO0 are not used as LED data terminals.
- Internal navigation away from the IO page asks whether active overrides should be released or intentionally kept active.

## 2026-05-01 (Machine-Setup IO Motorpolling Switch)
- Added an `ESP Motorpolling` checkbox to `/ui/machine-setup/io`.
- Added protected endpoints:
  - `GET /api/motors/poll` reads `MOTOR POLL?`.
  - `POST /api/motors/poll` writes `MOTOR POLL=1/0`.
- The ESP firmware contract is now explicit: enabled motor polling is a round-robin over motors `1..9` with `100 ms` minimum pause between individual motor polls, then wraps back to motor `1`.

## 2026-05-01 (Safety Stop + Reset Contract)
- Updated the machine runtime safety semantics:
  - Original commissioning polarity was active-high fault; superseded on 2026-06-02 by `HIGH=OK` / `LOW=fault`.
  - ESP `I0.7` handles hard Notaus.
  - ESP `I0.8` handles Lichtgitter and currently follows the same machine-state/reset behavior as Notaus.
- Safety activation latches `MAS0001=21` / `MAS0028=1`, blocks normal button transitions, sets the status lamp red and overrides Raspi button LEDs:
  - `Q0.0` and `Q0.2` alternate every second while latched/failed.
- Added the reset flow requested for commissioning:
  - trigger by `MAS0002=2` or Raspi `I0.7`
  - pulse ESP `Q0.2` as `200 ms HIGH / 1000 ms LOW / 200 ms HIGH / LOW`
  - verify ESP safety inputs are in their OK state
  - run ESP motor ETO recovery and alarm reset for motors `1..9`
  - run Smart-Wickler `stop`, `resetAlarm`, `etoRecovery`, `ready`
  - set `MAS0001=8` during reset and `MAS0001=9` when ready
- Added Raspi motor client commands for `MOTOR APPLY_ETO_RECOVERY` and `MOTOR RECOVER_ETO`.

## 2026-05-01 (Machine-Setup Motor UI Service Fixes)
- Hardened `/ui/machine-setup/motors` for commissioning:
  - motor command `NAK` replies from the ESP are now surfaced as UI/API errors instead of looking like silent success.
  - `Parameter speichern` now writes parameters, saves them and immediately refreshes the affected motor snapshot.
  - `Move mm`, `Schritte fahren`, zero/min/max and alarm reset update the affected motor card directly after the action.
  - added a per-motor `1s Polling` checkbox for targeted live refresh without globally refreshing all motor cards.
  - removed the default 2 second global motor-card refresh to prevent edited input fields from being overwritten while typing.
- Note for commissioning: if a `Move mm` command is rejected, the message field now shows the real ESP/AZD reason; common causes are active simulation, soft limits, missing ready state or safety/HWTO state.

## 2026-05-01 (Machine-Setup Motor Resolution Calibration Persistence)
- Improved `Aufloesung definieren` in `/ui/machine-setup/motors`.
- The direction confirmation now has explicit choices:
  - `Ja, korrekt`
  - `Nein, Richtung drehen`
  - `Abbrechen`
- If `Nein` is selected, the UI toggles the motor `invert_direction` setting.
- The calculated `steps/mm` and optional direction inversion are written and immediately saved through the persistent motor `SAVE` command.
- The calibration result is therefore the active persisted configuration used by the ESP32-PLC motor program flow; no extra `Parameter speichern` click is required after the calibration.

## 2026-05-08 (Parameter Workbook Microtom User Range)
- Updated the master-parameter workflow for the new workbook column `Microtom User Range` at column `G`.
- The column is treated as Microtom-only metadata and is intentionally not imported into the Databridge parameter schema.
- Workbook sync/enrichment now resolves columns by header name instead of fixed column numbers, so `R/W`, `ESP32 R/W`, `ZBC Mapping`, `Format relevant`, `Message`, `Remedy` and `KI-Anweisungen` remain correct after the one-column shift.
- Pulled production-only parameter rows into the master workbook and repo copy:
  - `MAE0031` Abwicklung falscher Kernadapter
  - `MAE0035` Aufwicklung falscher Kernadapter
- Refreshed KI entries in the shifted `KI-Anweisungen` column using the current project logic.
- Transient production runtime values such as live `MAS`/`MAE` states were not promoted to workbook defaults; they remain runtime state in `param_values`.

## 2026-05-11 (Machine-Setup Produktion)
- Added the protected Machine-Setup page `/ui/machine-setup/production`.
- The page lists all workbook/imported parameters marked as `Format relevant`, shows production status values such as machine state, product name, logfile flag and Wickler status, and lets commissioning users save named local format profiles.
- Saved profiles are persisted on the Raspi in the Databridge SQLite database table `format_profiles`.
- Sending a format uses the same Databridge Router path as Microtom/Testtool input instead of a separate shortcut, so existing parameter permissions, mappings, ACK/NAK behavior and downstream device fanout stay authoritative.
- Read-only/status values may be saved in a profile snapshot for visibility, but they are skipped when sending the profile to the machine.

## 2026-05-11 (Master Parameter Defaults From Production Motors)
- Imported the current edited master workbook as the active production parameter baseline.
- Synchronized the production motor commissioning values for Oriental motors `1..9` back into the repo master workbook where the workbook has a direct motor-related parameter:
  - `MAP0056..MAP0064` setpoint/default rows now use the production motor target defaults and configured software limits.
  - `MAS0011..MAS0017`, `MAS0031`, `MAS0032` actual-position rows now mirror the same commissioned ranges/defaults.
  - `MAE0004..MAE0010`, `MAE0046`, `MAE0047` remain default `0` with range `0..1`; current latched faults are runtime state and are not promoted to workbook defaults.
- Motor `3` remains a transport axis with neutral default `0`; its live distance counter is intentionally not stored as a workbook default.
- Production Raspi parameter import result: `758` rows updated.

## 2026-05-11 (Safety Reset ESP Process Latch)
- Hardened the Raspi safety reset path after production diagnosis showed that ESP process latches could reassert `MAE0027=1` / `MAS0028=1` immediately after an otherwise successful motor reset.
- Reset now sends `PROCESS RESET` to the ESP32-PLC after the ESP safety inputs are verified LOW and before recovering motors/Wicklers.
- Added an in-process reset lock so a UI-triggered reset and the background runtime loop cannot run the safety reset sequence concurrently.
- Smart Wicklers are no longer considered reset-ready solely because the AZD drive is electrically ready; the Raspi now also checks the Wickler logic state and reports faults such as `Stoerung / Wippe unten` as real reset blockers.

## 2026-05-12 (Production Raspi USB/NVMe Boot)
- Migrated only the production Raspi to USB/NVMe boot.
- Prepared Kingston SPSD 512 GB on `/dev/sda` with `MAS004BOOT` (`55740328-01`) and `MAS004ROOT` (`55740328-02`).
- Cloned the current production SD boot/root filesystems to the USB/NVMe target and verified the Databridge service after reboot.
- Updated the Pi 4 EEPROM tooling and configured `BOOT_ORDER=0xf14`, so USB/NVMe is tried before SD while SD remains fallback.
- Verified production runtime after reboot: `/` from `55740328-02`, `/boot` from `/dev/sda1`, `mas004-rpi-databridge.service` active, UI health OK.

## 2026-05-19 (Sensorbasierte Messfahrt mit Wickler-Durchmesseruebernahme)
- Der Einrichtablauf verwendet weiterhin die neue ESP-geführte, sensorreferenzierte Messfahrt statt eines starren 1000-mm-Relativkommandos.
- Der Raspi öffnet dabei wieder explizite `/api/diameter/learn`-Fenster auf beiden Wicklern.
- Die Durchmesserberechnung nutzt die vom ESP gemeldete absolut aufsummierte Einlauf-Encoderstrecke (`diameter_learn_travel_mm`) und den Wickler-Motorpuls-Akkumulator (`method=motor-accum`), nicht die Netto-Endposition der Hin-und-zurück-Fahrt.
- Nach erfolgreichem Lernen werden die Kandidaten direkt per `/api/diameter` persistent in beide Wickler geschrieben; ein fehlender absoluter Fahrweg bricht den Einrichtablauf bewusst ab, damit keine still falschen Rollendurchmesser übernommen werden.

## 2026-05-19 (Motor-Setup als Master)
- `/ui/machine-setup/motors` ist fuer Oriental-Motoren `1..9` die Master-Parametrierung, mit Motor `3` als Transportachsen-Sonderfall.
- `Parameter speichern` schreibt Konfiguration, Fahrstrom, Haltestrom, Min/Max und bei geaenderter Istposition auch den Positionsnullbezug auf den ESP und fuehrt danach `MOTOR <id> SAVE` aus.
- Erfolgreich gespeicherte Motorwerte werden in `motor_setup_master`, ParamStore und allen bekannten Master-Parameterlisten gespiegelt.
- Parameterlisten-Importe wenden anschliessend automatisch wieder den gespeicherten Motor-Setup-Master an, damit alte Excel-Werte keine Motorgrenzen oder Motorpositionen zurueckrollen.
- Automatische Stop-Positionsfahrten spielen vor der Bewegung die gespeicherten Motor-Setup-Grenzen und Stromwerte wieder auf den ESP, damit ein ESP-Neustart/Flash nicht mit alten Softlimits losfaehrt.

## 2026-05-19 (Einrichten Formatachsen als Positionssatz)
- Die Einricht-Formatachsen `ID5`, `ID6`, `ID7`, `ID8` und `ID9` werden nicht mehr in zwei wartenden Phasen positioniert.
- Der Raspi sendet alle Absolutziele als einen Positionssatz direkt nacheinander auf den ESP/Modbus-Pfad; erst danach wird gemeinsam verifiziert, ob alle Achsen ihre Zielposition erreicht haben.
- Die Sicherheitslogik bleibt erhalten: vor dem Senden werden Motor-Setup-Masterwerte angewendet und Min/Max, Alarm und HWTO je Achse geprueft.
- Die gemeinsame Positionspruefung pollt alle offenen Achsen im Round-Robin statt pro Achse blockierend bis Ziel.

## Maintenance Rule
- Add one entry for every change that affects:
  - architecture
  - deployment flow
  - API contracts
  - multi-repo sync behavior


