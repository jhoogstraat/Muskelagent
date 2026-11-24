# Entscheidungen (Stand 2026-11-24, ergänzt um Haushaltsartikel)

Grundlage ist die Spezifikation „Kitchen Agent – Systemdesign“. Leitlinie für jede
Entscheidung: Der Nutzer redet nur mit seinem Agenten, das System funktioniert ohne
Einrichtung und führt ihn. Wo die Spezifikation Aufwand ohne Nutzen für einen
Ein-Personen-Haushalt erzeugt, wird vereinfacht.

## Offene Entscheidungen der Spezifikation

| Frage | Entscheidung | Grund |
| --- | --- | --- |
| Wer bestätigt Pläne? | Immer der Nutzer, ein kurzes „passt“ genügt. Einkauf, Kochen und Vorrat bucht der Agent direkt; `undo_last_action` ist das Netz. | Pläne lösen Einkäufe aus; alles andere soll ohne Rückfrage laufen. |
| Angebotsquelle | Muskelmarkt über den vorhandenen Extractor, vom Server selbst täglich aktualisiert. Offerhopper später als zweiter Adapter. | Muskelmarkt funktioniert heute; für Offerhopper ist keine Schnittstelle bekannt. |
| Packungsgrößen | Aus dem Angebotsnamen („750g“, „4x 150g“, „6er“), sonst Preis ÷ Grundpreis. | Muskelmarkt liefert sie nicht strukturiert, beide Wege sind deterministisch. |
| Einkauf erfassen | Im Gespräch: „alles von der Liste außer Feta“. Ein Kassenbon-Foto überträgt die LLM-App in `record_purchase`. | Kein Händler-Export nötig. |
| Identität / Mandanten | Ein Haushalt, keine OAuth-Anmeldung. HTTP-Modus mit einem Geheimnis (`KITCHEN_TOKEN`): als Bearer-Token oder als Pfad `https://host/<token>/mcp`. | ChatGPT- und claude.ai-Connectoren können nur OAuth oder „keine Authentifizierung“; die geheime URL funktioniert mit beiden, OAuth wäre der größte Einzelaufwand ohne Nutzen. |
| Erreichbarkeit | Ziel: Container auf dem Pi hinter dem vorhandenen cloudflared, Connector-URL `https://<host>/<token>/mcp`. Bis dahin läuft der `tunnel-client` (stdio) auf dem Mac. | Der Pi läuft immer, der Mac schläft; cloudflared ist auf dem Pi schon da, der tunnel-client wird dann überflüssig. |
| Nährwerte | Eingebauter Katalog mit Richtwerten, Angebotsnährwerte, Angaben des Agenten. Jede Quelle wird ausgewiesen. | Ziele funktionieren ab dem ersten Plan. |
| Besuchskosten | Fester Betrag je Laden, Standard 1,50 €. | Bevorzugt wenige Läden ohne Geodaten. |

## Erweiterung: Haushaltsartikel

Klopapier, Spülmittel, Zahnpasta & Co. laufen über dieselben Tools wie Lebensmittel – kein eigenes Tool, damit der
Agent nichts Neues lernen muss.

- **Katalog**: 47 eingebaute Artikel (Papier, Küche, Wäsche, Reinigung, Körperpflege, Baby, Tier) mit Synonymen
  („Klopapier“, „Tabs“), Packungspreis und einer Richtzeit, wie lange eine Packung für eine Person hält. Unbekanntes legt
  der Agent mit `details.non_food` an. Haushaltsartikel haben keine Allergene, kein MHD und landen nie in Rezepten.
- **Gezählt in Packungen**: „8 Rollen“, „40 Tabs“, „20 WL“ oder „750 ml“ sind eine Packung; „2 Packungen“ sind zwei.
- **Rhythmus statt Bestand**: Niemand zählt Klopapierrollen. Der Server merkt sich den letzten Kauf und lernt, wie lange
  eine Packung im Haushalt hält (Start: Richtzeit ÷ Personen^0,8). Gelernt wird aus Nachkäufen, aus „ist alle“ (hielt bis
  heute) und aus „hab noch“ (hält länger). Vorgezogene Käufe (Angebot, Vorrat) zählen als Bestand, nicht als Verbrauch.
  „Eine Packung hält bei uns 3 Wochen“ setzt den Wert fest.
- **Von selbst auf die Liste**: Wird ein Artikel vor dem nächsten Einkauf fällig, kommt er beim Bestätigen eines Plans oder
  bei jeder Listenänderung mit auf die Liste – als „zu Hause prüfen“ (nicht in der Summe). „Ist alle/fast leer“ setzt ihn
  sofort fest drauf, notfalls auf eine neue Liste. Dasselbe gilt jetzt für knappe Grundzutaten wie Öl oder Salz.
- **„Hab noch“**: Bleibt ein Prüf-Posten beim Einkauf ungekauft oder wird er von der Liste gestrichen, fragt der Server
  frühestens nach einer Woche wieder und verlängert den Rhythmus etwas.
- **Lesende Tools schreiben nicht**: Status und Einkaufsliste zeigen fällige Artikel als `restock_suggestions`; eingetragen
  werden sie erst beim nächsten schreibenden Aufruf. Sonst würde „mach das rückgängig“ die Listenpflege statt der letzten
  Nutzeraktion zurücknehmen.
- **Angebote**: Muskelmarkt führt nur Lebensmittel; Preise für Haushaltsartikel kommen aus dem Katalog und den eigenen Kassenbons.
- **Datenbank**: Schema-Version 2 ergänzt `supply_state` und zwei Spalten; bestehende Datenbanken werden beim Start
  migriert, Artikel, die früher als „Lebensmittel“ angelegt wurden, werden umgestellt.

## Abweichungen von der Spezifikation

- **Speicher**: SQLite aus der Standardbibliothek mit eigener Schema-Version statt SQLAlchemy/Alembic. Weniger bewegliche Teile.
- **Läden** sind Ketten („Lidl“), keine Filialen – so liefert Muskelmarkt die Angebote.
- **Lagerort** bleibt an der Charge (`fridge`, `freezer`, `pantry`): Tiefkühlen verlängert die Haltbarkeit.
- **Angebote ↔ Zutaten**: Jedes Angebot wird genau einer Zutat zugeordnet, dem längsten Katalogbegriff in seinem Namen („Milchreis“ schlägt „Reis“).
- **Rezepte**: Fehlen gespeicherte Rezepte, schlägt der Agent welche vor und übergibt sie mit Zutaten an `plan_meals`; der Server rechnet und speichert sie.
- **Entwürfe** liegen als berechneter Vorschlag am Plan; erst `confirm_plan` legt Mahlzeiten, Trips, Positionen und Reservierungen an. Ein bestätigter Plan kann per `revise_plan` geändert werden; erneutes `confirm_plan` ersetzt seine Zukunft, Gekauftes und Gekochtes bleibt.
- **Idempotenz**: Der Server erkennt identische Kommandos innerhalb von 2 Minuten selbst; die LLM-App muss keinen Schlüssel schicken.
- **Undo** ab dem ersten Release über ein Änderungsprotokoll (Trigger), nicht erst in Phase 4.
- **Zusätzliche Intent-Tools**: `find_offers` (die bisherige Angebotssuche) und `update_shopping_list` („setz Milch auf die Liste“).
- **Tagesziele bei Teilplänen**: Ein Plan nur fürs Abendessen wird gegen den Anteil der Mahlzeit am Tagesziel geprüft (Frühstück 25 %, Mittag 35 %, Abend 35 %, Snack 5 %).

## Tests

Nur End-to-End: `tests/e2e` startet den echten Server als Prozess (stdio und HTTP) und
spielt Alltagsszenarien über MCP durch. `tests/agent` lässt Claude selbst mit dem Server
sprechen und prüft, ob Alltagssätze zum richtigen Ablauf führen.
