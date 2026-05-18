# Motor-Positionssicherheit

Stand: 2026-05-18

## Grundregel

Die Positionsachsen ID1, ID2 und ID4 bis ID9 duerfen ihre aktiven Min-/Max-Grenzen nie automatisch verlassen. Motor ID3 ist ausgenommen, weil er als Transportachse nicht als Format-Positionierachse betrieben wird.

## Schutzebenen

1. Die ESP32-PLC prueft vor jeder Absolut-, Relativ- oder Schrittbewegung die aktuelle Istposition, den Zielwert, Alarmstatus und HWTO/Sicherheitskreis. Ist die Achse bereits ausserhalb der aktiven Min-/Max-Grenzen, wird kein Fahrbefehl ausgefuehrt.
2. Die aktiven Min-/Max-Grenzen werden fuer Positionsachsen beim Laden der Konfiguration erzwungen. `min_enabled=false` oder `max_enabled=false` wird fuer Positionsachsen abgelehnt.
3. Beim Speichern oder Aendern der Motorparameter schreibt die ESP32-PLC die Softwarelimits in den AZD-Controller. Damit kennt auch der Drive die aktuelle positive und negative Softwaregrenze.
4. Die Raspi-Automatik prueft vor Stop-Positionsfahrten live `CONFIG` und `REFRESH`. Bei Alarm, HWTO, Istposition ausserhalb Limit oder Ziel ausserhalb Limit werden weder `RESET_ALARM` noch `MOVE_ABS_MM` automatisch gesendet.
5. Die Weboberflaeche blockiert manuelle Bewegungsbefehle fuer Positionsachsen, wenn aktuelle Istposition oder Zielwert ausserhalb aktiver Min-/Max-Grenzen liegen.

## Recovery-Hinweis

Wenn eine Achse bereits ausserhalb ihres Grenzbereichs steht, darf sie nicht automatisch freigefahren werden. Die Ursache muss mechanisch und elektrisch geprueft werden. Erst danach darf im Machine-Setup bewusst referenziert bzw. die Position/Grenze korrigiert werden.
