# Motor-Positionssicherheit

Stand: 2026-06-25

## Grundregel

Die Positionsachsen ID1, ID2 und ID4 bis ID9 duerfen ihre aktiven Min-/Max-Grenzen nie automatisch verlassen. Motor ID3 ist ausgenommen, weil er als Transportachse nicht als Format-Positionierachse betrieben wird.

## Schutzebenen

1. Die ESP32-PLC prueft vor jeder Absolut-, Relativ- oder Schrittbewegung die aktuelle Istposition, den Zielwert, Alarmstatus und HWTO/Sicherheitskreis. Ist die Achse bereits ausserhalb der aktiven Min-/Max-Grenzen, wird kein Fahrbefehl ausgefuehrt.
2. Die aktiven Min-/Max-Grenzen werden fuer Positionsachsen beim Laden der Konfiguration erzwungen. `min_enabled=false` oder `max_enabled=false` wird fuer Positionsachsen abgelehnt.
3. Beim Speichern oder Aendern der Motorparameter schreibt die ESP32-PLC die Softwarelimits in den AZD-Controller. Damit kennt auch der Drive die aktuelle positive und negative Softwaregrenze.
4. Die Raspi-Automatik prueft vor Stop-Positionsfahrten live `CONFIG` und `REFRESH`. Bei Alarm, HWTO, Istposition ausserhalb Limit oder Ziel ausserhalb Limit werden weder `RESET_ALARM` noch `MOVE_ABS_MM` automatisch gesendet.
5. Die Weboberflaeche blockiert manuelle Bewegungsbefehle fuer Positionsachsen, wenn aktuelle Istposition oder Zielwert ausserhalb aktiver Min-/Max-Grenzen liegen.
6. `SET_POSITION_MM`, `ZERO`, `SET_MIN`, `SET_MAX` sowie geschuetzte Konfigurationsfelder wie `zero_offset_steps`, `min_tenths_mm` und `max_tenths_mm` sind fuer ID1/2/4-9 im Raspi-ESP-Client nur mit expliziter Machine-Setup-Schreibfreigabe erlaubt. Runtime, Stop-Mode, Einrichten, Excel-/DB-Import und Statusabfragen duerfen diese Werte nicht auf den ESP zurueckschreiben.
7. Alte automatische Positions-Restore-Ereignisse machen die betroffene Achse fuer automatische Positionsfahrten verdachtsbehaftet, bis sie ueber `/ui/machine-setup/motors` neu kalibriert und gespeichert wurde.

## Recovery-Hinweis

Wenn eine Achse bereits ausserhalb ihres Grenzbereichs steht, darf sie nicht automatisch freigefahren werden. Die Ursache muss mechanisch und elektrisch geprueft werden. Erst danach darf im Machine-Setup bewusst referenziert bzw. die Position/Grenze korrigiert werden.

Ein Positionszaehler darf fuer Positionsachsen nie automatisch aus `motor_setup_master`, Excel, DB-Defaults oder Runtime-Historie rekonstruiert werden. Wenn die UI-Position plausibel wirkt, die reale Mechanik aber nicht dazu passt, ist die UI-Position als unsicher zu behandeln und die Achse muss manuell im Machine-Setup neu gesetzt werden.
