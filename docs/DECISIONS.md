# Entscheidungen (Stand 2026-11-24)

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
| Identität / Mandanten | Ein Haushalt, keine OAuth-Anmeldung. HTTP-Modus optional mit Bearer-Token. | Ein Haushalt; OAuth wäre der größte Einzelaufwand ohne Nutzen. |
| Erreichbarkeit | Der bestehende `tunnel-client` (stdio) bleibt der Weg. Streamable HTTP und Docker liegen für den Pi bereit. | Funktioniert heute schon. |
| Nährwerte | Eingebauter Katalog mit Richtwerten, Angebotsnährwerte, Angaben des Agenten. Jede Quelle wird ausgewiesen. | Ziele funktionieren ab dem ersten Plan. |
| Besuchskosten | Fester Betrag je Laden, Standard 1,50 €. | Bevorzugt wenige Läden ohne Geodaten. |

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
