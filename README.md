# Stromvertragsanalyse für Home Assistant

Ein lokales Home-Assistant-Add-on mit Ingress-Oberfläche zur Auswertung von Stromverträgen über Kalenderjahre. Verträge und Ablesungen liegen in einer SQLite-Datei im persistenten `/data`-Verzeichnis des Add-ons.

## Installation

Das Verzeichnis `stromvertrag` als lokales Add-on unter `/addons/stromvertrag` in Home Assistant ablegen, den Add-on-Store neu laden, **Stromvertragsanalyse** installieren und starten. Die Oberfläche über **Web-UI öffnen** aufrufen. Das Add-on benötigt Home Assistant mit Supervisor und Recorder. Es nutzt die [Core-API über den Supervisor-Proxy](https://contributors.home-assistant.io/apps/guides/communication/) und den [Recorder-Dienst `get_statistics`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/recorder/services.py). Ein Container-only-Setup ohne Supervisor unterstützt diesen Installationsweg nicht.

## Verwendung

1. Vertrag mit Anbieter, Tarif, Start- und Enddatum, Grundgebühr, Arbeitspreis, Abschlussmenge und bis zu drei Boni anlegen. Die Grundgebühr kann als Jahres- oder Monatsbetrag eingegeben werden; beim Wechsel der Auswahl wird der vorhandene Betrag umgerechnet. Optional die Summe der tatsächlich gezahlten Abschläge eintragen.
2. Optional einen kumulativen Energie-Sensor auswählen, der Langzeitstatistiken liefert. Derselbe Sensor kann im Energy Dashboard verwendet werden. Die Statistik wird in kWh angefragt.
3. Für abgeschlossene Verträge ohne vollständige Sensordaten im Vertragsformular den Zählerstand am Vertragsbeginn und am **Tag nach Vertragsende** eingeben. Bei Verträgen über mehrere Jahre werden fehlende Jahresanteile nach Kalendertagen geschätzt und entsprechend markiert. Vorhandene vollständige Jahresstatistiken bleiben erhalten.
4. Für exakte Jahreswerte unter **Bearbeiten → Weitere Ablesungen** zusätzlich die Zählerstände am jeweiligen Jahreswechsel eintragen. Für einen Jahreszeitraum werden **beide** Grenzstände benötigt; deren Differenz ersetzt dann die Statistik dieses Zeitraums. Die Vertragsübersicht zeigt nur Ergebnisse.

Im Vertragsformular zeigt **Voraussichtliche Vertragskosten** laufend die Gesamtkosten auf Basis der vereinbarten kWh-Menge und der eingetragenen Preise und Boni. Das ist eine Planung; die Ist-Kosten in der Übersicht verwenden Messdaten oder manuelle Zählerstände. Die Vertragsübersicht zeigt die neuesten Vertragsbeginne zuerst. Bei abgeschlossenen Verträgen heißen die Summen „Verbrauch gesamt“ und „Effektive Gesamtkosten“. Die Icons in „Datenlage“ zeigen ihren genauen Status als Tooltip und für Screenreader an.

Die Jahreswerte lassen sich pro Vertrag über **Jahresübersicht** mit dem Pfeil rechts einblenden; beim Öffnen der Seite bleiben sie zunächst verborgen. Darunter stehen fünf Werte auf breiten Bildschirmen in einer Zeile: Verbrauch, effektive Kosten, **effektive Kosten je ausgewerteter kWh** (effektive Kosten geteilt durch Verbrauch), gezahlte Abschläge und der Saldo. Dieser ist **effektive Kosten einschließlich Grundgebühr und nach Boni minus gezahlte Abschläge**. Ein positiver Saldo ist eine Nachzahlung, ein negativer Saldo ein Guthaben. Für laufende Verträge, unvollständige Messdaten oder geschätzte Jahresanteile ist der Saldo als vorläufig gekennzeichnet. Ohne eingetragene Abschläge oder ohne berechenbare Ist-Kosten bleibt der Saldo leer. Bei Lücken oder geschätzten Jahresanteilen steht dort ein entsprechender Hinweis. Eingeklappte Vertragskarten zeigen Verbrauch, effektive Kosten und den effektiven kWh-Preis in einer einheitlich ausgerichteten Kurzansicht.

Die Oberfläche übernimmt im Home-Assistant-Ingress die aktiven Primär- und Akzentfarben sowie die passenden Hintergrund- und Textfarben aus dem Home-Assistant-Theme. Ein Wechsel des Themes wird ohne Neuladen übernommen. Außerhalb von Home Assistant werden lokale Standardfarben verwendet.

Die Jahresauswertung verwendet stündliche `change`-Werte aus der Recorder-Langzeitstatistik. Fehlen Stunden, erscheint **Statistik (unvollständig)**. Fehlen alle Messwerte, bleiben Verbrauch und verbrauchsabhängige Kosten leer. Manuelle Ablesungen müssen in derselben Einheit (kWh) und vom selben Zähler stammen.

Eine jährliche Grundgebühr wird nach Kalendertagen des jeweiligen Jahres, eine monatliche nach Kalendertagen des jeweiligen Monats aufgeteilt. Boni werden linear nach Vertragstagen aufgeteilt; dies ist eine rechnerische Zuordnung, nicht der tatsächliche Auszahlungszeitpunkt. **Brutto** ist Grundgebühr plus Verbrauchskosten, **Effektiv** zieht die zugeordneten Boni ab. Die Abschlussmenge dient als Vertragsinformation und wird nicht als gemessener Verbrauch ausgegeben.

## Update und Datenerhalt

Version `0.9.0` ändert nur die Oberfläche und verwendet dieselbe Datenbank und dieselben Tabellen wie die Vorversionen. Alte Verträge und Ablesungen bleiben erhalten. Bestehende Grundgebühren werden weiterhin als Monatsbetrag behandelt; neue Verträge beginnen mit der Auswahl „Pro Jahr“. Das Feld für gezahlte Abschläge bleibt bei bestehenden Verträgen erhalten. Alle Ablesungen werden im Bearbeitungsbereich geändert. Leere Grenzstandfelder löschen nur einen zuvor eingetragenen Grenzstand an genau diesem Datum; andere Ablesungen bleiben bestehen. Vor dem Update empfiehlt sich ein Home-Assistant-Backup des Add-ons. Das Add-on wird für ein konsistentes Backup kurz angehalten (`backup: cold`).

Verträge werden nach Vertragsbeginn (neueste zuerst) angezeigt. Die Datumsanzeige nutzt die Sprache der Home-Assistant-Systemkonfiguration, z. B. `de` für `31.12.2025`. Die Eingabefelder für Daten sind native Browser-Datumsfelder; deren Kalenderdarstellung kann zusätzlich von der Browser- oder Gerätesprache abhängen.

Bei Installation aus einem Git-Repository: Änderungen in **dasselbe** öffentliche Repository und auf denselben Branch hochladen. Die erhöhte `version` in `stromvertrag/config.yaml` macht das Update nach Neuladen des App-Stores sichtbar. In Home Assistant unter **Einstellungen → Apps** das bestehende Add-on öffnen und **Aktualisieren** wählen. `/data/contracts.sqlite3` bleibt beim Update erhalten. Bei einer lokalen Installation die Dateien im vorhandenen lokalen Add-on-Verzeichnis ersetzen, den App-Store neu laden und ebenfalls das Update der bestehenden App ausführen.

Wichtig: Git-Repository-Installation und lokale Installation haben unterschiedliche App-IDs. Eine Neuinstallation unter einer anderen Repository-URL oder als lokale App übernimmt die Daten der alten Installation nicht automatisch. Ebenso muss der `slug: stromvertrag` gleich bleiben.

## Entwicklung

Modelltests: `python3 -m unittest discover -s stromvertrag/tests -v`

Lokaler Start ohne Home Assistant: `LOCAL_DEV=1 DATA_DIR=/tmp/stromvertrag-data python3 stromvertrag/app/server.py`. Für echte Sensoren und Auswertungen ist eine Supervisor-Verbindung mit `SUPERVISOR_TOKEN` nötig.
