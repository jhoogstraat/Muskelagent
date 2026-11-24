"""MCP facade: intent tools with guiding descriptions, over stdio or Streamable HTTP."""

from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import sys
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from pydantic import BaseModel, Field

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .db import KitchenError
from .status import GUIDE, Kitchen

DEFAULT_DB = Path.home() / ".local" / "share" / "muskelagent" / "kitchen.sqlite3"
API_VERSION = "2.0"
log = logging.getLogger("kitchen")


# --- input models ---------------------------------------------------------------------------
class Item(BaseModel):
    name: str = Field(description="Zutat, gern mit Menge: '500 g Hähnchenbrust', '2 Zwiebeln', 'Feta'")
    qty: float | None = Field(None, description="Menge, falls nicht im Namen")
    unit: str | None = Field(None, description="g, kg, ml, l, Stück, EL, TL, Packung, Dose, Becher …")
    details: dict[str, Any] | None = Field(None, description="Nur für neue, unbekannte Zutaten: allergens (Liste), animal "
                                                             "(meat/fish/dairy/egg/null), category, kcal, protein_g, carbs_g, fat_g pro 100 g. "
                                                             "Unbekannter Haushaltsartikel (kein Lebensmittel): {non_food: true}")


class PantryItem(Item):
    action: Literal["add", "set", "use", "discard", "move", "level"] = Field(
        "add", description="add=dazu, set=genaue Ist-Menge, use=verbraucht, discard=weggeworfen, move=Lagerort wechseln "
                           "(z. B. einfrieren), level=Füllstand Grundzutat")
    location: str | None = Field(None, description="Kühlschrank, Tiefkühler oder Vorrat; sonst üblicher Ort der Zutat")
    best_before: str | None = Field(None, description="MHD, z. B. '2026-12-03' oder '3.12.'; ohne Angabe schätzt der Server")
    level: str | None = Field(None, description="Für Grundzutaten (Salz, Öl) und Haushaltsartikel (Klopapier): voll, knapp, leer")
    lasts_days: float | None = Field(None, description="Nur Haushaltsartikel: so viele Tage hält eine Packung bei uns")
    track: bool | None = Field(None, description="Nur Haushaltsartikel: false = nicht mehr ans Nachkaufen erinnern")


class BoughtItem(Item):
    price_eur: float | None = Field(None, description="Bezahlter Preis für diese Position, wenn bekannt")
    best_before: str | None = None
    location: str | None = None


class Recipe(BaseModel):
    name: str
    servings: float = Field(2, description="Für wie viele Portionen die Mengen gelten")
    ingredients: list[str | Item] = Field(description="Zutaten mit Mengen für `servings` Portionen, z. B. '300 g Spaghetti'")
    prep_min: int | None = None
    cook_min: int | None = None
    tags: list[str] | None = Field(None, description="z. B. vegetarisch, schnell, meal-prep, frühstück")
    instructions: str | None = Field(None, description="Kurze Zubereitung")
    cooked_shelf_life_days: float | None = Field(None, description="Wie lange Reste halten (Standard 3)")


class Meal(BaseModel):
    recipe: str | Recipe | None = Field(None, description="Name eines gespeicherten Rezepts, ein neues Rezept-Objekt, 'Reste', "
                                                          "'auto' (Server wählt) oder 'nichts' (Termin frei lassen)")
    date: str | None = Field(None, description="Tag: '2026-11-25', 'morgen', 'Dienstag'. Ohne Datum: nächster freier Termin")
    meal_type: str | None = Field(None, description="Frühstück, Mittagessen, Abendessen, Snack")
    servings: float | None = Field(None, description="Nur wenn abweichend von der Haushaltsgröße")


class Member(BaseModel):
    name: str
    portion_factor: float | None = Field(None, description="1 = normale Portion, 0.5 = Kind, 1.3 = großer Hunger")
    remove: bool = False


class Restriction(BaseModel):
    kind: Literal["allergy", "intolerance", "diet", "exclude"] = Field(
        description="allergy/intolerance: z. B. Laktose, Gluten, Nüsse; diet: vegetarisch, vegan, pescetarisch; exclude: Zutat/Kategorie wie Pilze")
    value: str
    member: str | None = Field(None, description="Name des Mitglieds; leer = ganzer Haushalt")
    remove: bool = False


class Target(BaseModel):
    nutrient: str = Field(description="kcal, protein, kohlenhydrate, fett")
    daily_min: float | None = None
    daily_max: float | None = None
    member: str | None = None


class StorePref(BaseModel):
    name: str = Field(description="Kette, z. B. Lidl, Rewe, Aldi Süd")
    stance: Literal["preferred", "neutral", "avoid"] = "preferred"
    visit_cost_eur: float | None = Field(None, description="Aufwand eines Besuchs in Euro (Standard 1,50)")
    remove: bool = False


class Preference(BaseModel):
    value: str = Field(description="Zutat, Kategorie oder Stichwort, z. B. 'Pilze', 'scharf', 'Fisch'")
    like: bool = True
    kind: Literal["ingredient", "tag"] = "ingredient"
    remove: bool = False


def _dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_none=True)
    if isinstance(value, list):
        return [_dump(v) for v in value]
    return value


INSTRUCTIONS = f"""Kitchen Agent: plant Mahlzeiten, Einkäufe und Vorrat eines Haushalts. API {API_VERSION}.
{GUIDE}"""


def build_server(kitchen: Kitchen) -> MCPServer:
    mcp = MCPServer(name="kitchen-agent", title="Kitchen Agent", version=API_VERSION, instructions=INSTRUCTIONS)
    read = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
    write = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

    def guarded(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                result = fn(*args, **kwargs)
                today = kitchen.today()
                result["today"] = f"{today.isoformat()} ({kitchen.day_label(today).split('(')[-1].rstrip(')')})"
                return result
            except KitchenError as err:
                payload = {"error": {"code": err.code, "message": err.message, "details": err.details},
                           "ask_user": err.ask_user, "next_actions": err.next_actions}
                raise ToolError(json.dumps(payload, ensure_ascii=False, default=str)) from err
            except ToolError:
                raise
            except Exception as err:  # Unexpected: keep the server alive and tell the agent what happened.
                log.exception("tool %s failed", fn.__name__)
                raise ToolError(json.dumps({"error": {"code": "internal_error", "message": f"{type(err).__name__}: {err}"}},
                                           ensure_ascii=False)) from err
        return wrapper

    @mcp.tool(annotations=read)
    @guarded
    def get_kitchen_status() -> dict[str, Any]:
        """Überblick: was heute und demnächst ansteht. Rufe dies zu Beginn JEDES Gesprächs über Essen, Einkauf oder Vorrat auf.
        Liefert nächste Mahlzeiten, offene Einkäufe, bald Ablaufendes, Reste, Rückfragen (ask_user) und einen Kurzleitfaden.
        Folge danach next_actions. Nicht für Details zum Vorrat (get_pantry) oder zur Einkaufsliste (get_shopping_list)."""
        return kitchen.get_kitchen_status()

    @mcp.tool(annotations=write)
    @guarded
    def plan_meals(
        days: Annotated[int | None, Field(description="Anzahl Tage, Standard 7")] = None,
        start: Annotated[str | None, Field(description="Erster Tag, z. B. 'morgen', 'Montag'; Standard heute (nachmittags: morgen)")] = None,
        meal_types: Annotated[list[str] | None, Field(description="z. B. ['Abendessen'] oder ['Mittagessen','Abendessen']")] = None,
        meals: Annotated[list[Meal] | None, Field(description="Feste Wünsche oder deine Rezeptvorschläge; Rest wählt der Server")] = None,
        budget_eur: Annotated[float | None, Field(description="Höchstbetrag für den Einkauf dieses Plans")] = None,
        max_stores: Annotated[int | None, Field(description="Höchstens so viele Läden pro Einkauf")] = None,
        stores: Annotated[list[str] | None, Field(description="Nur in diesen Läden einkaufen")] = None,
        shopping_days: Annotated[list[str] | None, Field(description="Einkaufstage; ohne Angabe plant der Server sie")] = None,
        servings: Annotated[float | None, Field(description="Portionen pro Mahlzeit, falls abweichend vom Haushalt")] = None,
    ) -> dict[str, Any]:
        """Erstellt einen Wochen-/Mahlzeitenplan als ENTWURF inkl. Einkaufsliste mit Angeboten, Packungsgrößen und Kosten.
        Nutze dies bei 'Plan mir die Woche', 'Was essen wir die nächsten Tage'. Der Server nutzt Vorrat und Reste zuerst,
        beachtet Allergien und Budget und rechnet alle Mengen und Preise. Fehlen gespeicherte Rezepte, schlage selbst
        Gerichte vor und übergib sie in meals mit Zutaten. Zeige den Vorschlag (Gerichte je Tag, Einkauf je Laden, Summe)
        und rufe confirm_plan ERST nach Zustimmung auf. Für Änderungen: revise_plan."""
        return kitchen.plan_meals({"days": days, "start": start, "meal_types": meal_types, "meals": _dump(meals),
                                   "budget_eur": budget_eur, "max_stores": max_stores, "stores": stores,
                                   "shopping_days": shopping_days, "servings": servings})

    @mcp.tool(annotations=write)
    @guarded
    def revise_plan(
        proposal_id: Annotated[str | None, Field(description="proposal_id aus plan_meals/revise_plan; leer = neuester Plan")] = None,
        replace_meals: Annotated[list[Meal] | None, Field(description="Termine ersetzen (date nötig); recipe 'auto' = anderes Gericht wählen, 'Reste', 'nichts'")] = None,
        meals: Annotated[list[Meal] | None, Field(description="Zusätzliche Wünsche in freie Termine")] = None,
        days: int | None = None,
        start: str | None = None,
        meal_types: list[str] | None = None,
        budget_eur: float | None = None,
        max_stores: Annotated[int | None, Field(description="z. B. 1 für 'nur ein Laden'")] = None,
        stores: Annotated[list[str] | None, Field(description="Nur diese Läden; leere Liste = alle erlaubten")] = None,
        shopping_days: list[str] | None = None,
        servings: float | None = None,
    ) -> dict[str, Any]:
        """Ändert einen Planvorschlag oder einen bereits bestätigten Plan: 'Dienstag lieber vegetarisch', 'nur ein Laden',
        'Reste am Donnerstag', 'billiger'. Rechnet Bedarf, Einkauf und Kosten neu und liefert eine neue proposal_id.
        Andere Tage bleiben unverändert. Danach Ergebnis zeigen und nach Zustimmung confirm_plan aufrufen
        (auch bei bereits bestätigten Plänen, damit Einkaufsliste und Reservierungen angepasst werden)."""
        return kitchen.revise_plan({"proposal_id": proposal_id, "replace_meals": _dump(replace_meals), "meals": _dump(meals),
                                    "days": days, "start": start, "meal_types": meal_types, "budget_eur": budget_eur,
                                    "max_stores": max_stores, "stores": stores, "shopping_days": shopping_days, "servings": servings})

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    @guarded
    def confirm_plan(proposal_id: Annotated[str, Field(description="proposal_id des Vorschlags, dem der Nutzer zugestimmt hat")]) -> dict[str, Any]:
        """Bestätigt einen Planvorschlag, nachdem der Nutzer zugestimmt hat ('passt', 'ja, so machen'). Legt Mahlzeiten,
        Einkaufsliste und Reservierungen im Vorrat an. Prüft Allergien und Regeln und lehnt Verstöße ab.
        Nicht ohne Zustimmung des Nutzers aufrufen. Danach: get_shopping_list bei Bedarf."""
        return kitchen.confirm_plan(proposal_id)

    @mcp.tool(annotations=read)
    @guarded
    def get_shopping_list(
        store: Annotated[str | None, Field(description="Nur dieser Laden, z. B. 'Lidl'")] = None,
        date: Annotated[str | None, Field(description="Nur dieser Einkaufstag")] = None,
    ) -> dict[str, Any]:
        """Zeigt die Einkaufsliste je Laden, nach Abschnitten sortiert, mit Packungen, Preisen und Angeboten.
        Nutze dies bei 'Was muss ich kaufen?', 'Was hole ich bei Lidl?'. Zum Ändern: update_shopping_list."""
        return kitchen.get_shopping_list(store, date)

    @mcp.tool(annotations=write)
    @guarded
    def update_shopping_list(
        add: Annotated[list[str | Item] | None, Field(description="z. B. ['Milch', '2 Packungen Haferflocken']")] = None,
        remove: Annotated[list[str] | None, Field(description="Namen, die von der Liste sollen")] = None,
        store: Annotated[str | None, Field(description="Laden, auf dessen Liste es soll")] = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Setzt Dinge auf die Einkaufsliste oder streicht sie: 'Setz Milch auf die Liste', 'Klopapier mit drauf',
        'Brot brauche ich nicht'. Der Server sucht Angebote und Packungsgrößen und ergänzt bald nötige Haushaltsartikel
        selbst. Streichen eines solchen Artikels heißt 'noch genug da'. Nicht für geplante Gerichte (plan_meals)."""
        return kitchen.update_shopping_list(_dump(add), remove, store, date)

    @mcp.tool(annotations=write)
    @guarded
    def record_purchase(
        rest_of_list_bought: Annotated[bool | None, Field(description="true = alles Übrige von der Liste gekauft (Standard, wenn bought leer)")] = None,
        missing: Annotated[list[str] | None, Field(description="Was es nicht gab / nicht gekauft wurde")] = None,
        bought: Annotated[list[str | BoughtItem] | None, Field(description="Abweichungen oder Extras mit Menge/Preis, z. B. Kassenbon-Positionen")] = None,
        store: str | None = None,
        date: Annotated[str | None, Field(description="Einkaufstag, Standard heute")] = None,
        total_eur: Annotated[float | None, Field(description="Summe laut Kassenbon")] = None,
    ) -> dict[str, Any]:
        """Bucht einen Einkauf: 'War einkaufen, alles außer Feta', 'Hähnchen 1,2 kg für 11,90 €', oder Positionen eines
        Kassenbon-Fotos, auch Drogerie- und Haushaltsartikel. Gleicht mit der Einkaufsliste ab, legt alles in den Vorrat
        (mit geschätztem MHD), speichert Preise.
        Fehlt etwas für geplante Gerichte, kommt eine Rückfrage (Ersatz, tauschen, ohne). Nicht für Dinge, die schon
        zu Hause waren (update_pantry)."""
        return kitchen.record_purchase({"rest_of_list_bought": rest_of_list_bought, "missing": missing, "bought": _dump(bought),
                                        "store": store, "date": date, "total_eur": total_eur})

    @mcp.tool(annotations=write)
    @guarded
    def record_cooking(
        recipe: Annotated[str | None, Field(description="Was gekocht wurde; leer = die heute geplante Mahlzeit")] = None,
        date: str | None = None,
        meal_type: str | None = None,
        servings_cooked: Annotated[float | None, Field(description="Gekochte Portionen, falls anders als geplant")] = None,
        servings_eaten: float | None = None,
        leftover_servings: Annotated[float | None, Field(description="Übrige Portionen → werden als Reste gespeichert")] = None,
        rating: Annotated[int | None, Field(ge=1, le=5, description="Nur wenn der Nutzer eine Bewertung äußert")] = None,
        comment: str | None = None,
        ingredients: Annotated[list[str | Item] | None, Field(description="Nur bei ungeplanten, unbekannten Gerichten: verbrauchte Zutaten")] = None,
    ) -> dict[str, Any]:
        """Bucht eine gekochte oder gegessene Mahlzeit: 'Curry gekocht, zwei Portionen übrig', 'Hab gestern das geplante
        Essen gekocht', 'Reste gegessen'. Direkt aufrufen, ohne Rückfrage: ohne recipe nimmt der Server die geplante
        Mahlzeit des Tages (date wörtlich, z. B. 'gestern'). Zieht Zutaten vom Vorrat ab (reservierte zuerst) und legt
        übrige Portionen als Reste an, die der Planer später einplant. Nicht für ausgelassene Mahlzeiten (skip_meal)."""
        return kitchen.record_cooking({"recipe": recipe, "date": date, "meal_type": meal_type, "servings_cooked": servings_cooked,
                                       "servings_eaten": servings_eaten, "leftover_servings": leftover_servings, "rating": rating,
                                       "comment": comment, "ingredients": _dump(ingredients)})

    @mcp.tool(annotations=write)
    @guarded
    def skip_meal(
        date: Annotated[str | None, Field(description="Tag der geplanten Mahlzeit, Standard heute")] = None,
        meal_type: str | None = None,
        recipe: Annotated[str | None, Field(description="Name des geplanten Gerichts, falls der Tag unklar ist")] = None,
        move_to: Annotated[str | None, Field(description="Statt auslassen auf diesen Tag verschieben (tauscht bei Bedarf)")] = None,
        move_to_meal_type: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Lässt eine geplante Mahlzeit aus oder verschiebt sie: 'Samstag essen wir auswärts', 'Curry lieber morgen'.
        Gibt reservierte Zutaten frei, nimmt nicht mehr Benötigtes von der Einkaufsliste und schlägt Verwertung vor."""
        return kitchen.skip_meal({"date": date, "meal_type": meal_type, "recipe": recipe, "move_to": move_to,
                                  "move_to_meal_type": move_to_meal_type, "reason": reason})

    @mcp.tool(annotations=read)
    @guarded
    def get_pantry(
        query: Annotated[str | None, Field(description="Suchtext, z. B. 'Käse'")] = None,
        location: Annotated[str | None, Field(description="Kühlschrank, Tiefkühler oder Vorrat")] = None,
        expiring_within_days: Annotated[int | None, Field(description="Nur was in so vielen Tagen abläuft")] = None,
    ) -> dict[str, Any]:
        """Zeigt den Vorrat, bald Ablaufendes zuerst, mit Lagerort, Reservierungen für geplante Gerichte und Resten,
        dazu Haushaltsartikel mit geschätzter Reichweite. Nutze dies bei 'Was haben wir noch?', 'Was läuft bald ab?',
        'Haben wir Eier?', 'Wie lange reicht das Klopapier?'."""
        return kitchen.get_pantry(query, location, expiring_within_days)

    @mcp.tool(annotations=write)
    @guarded
    def update_pantry(
        items: Annotated[list[PantryItem], Field(description="Änderungen am Vorrat")],
        stocktake_location: Annotated[str | None, Field(description="Inventur: alles an diesem Ort, das nicht in items steht, ist aufgebraucht")] = None,
    ) -> dict[str, Any]:
        """Ändert den Vorrat ohne Einkauf oder Kochen: 'Wir haben noch 6 Eier' (set), 'Hab 200 g Käse gegessen' (use),
        'Milch ist schlecht' (discard), 'Hähnchen eingefroren' (move nach Tiefkühler), 'Salz ist fast leer' oder
        'Spülmittel ist alle' (level; kommt dann von selbst auf die Liste), 'Eine Packung Klopapier hält bei uns 3 Wochen'
        (lasts_days). Einkäufe mit record_purchase, Gekochtes mit record_cooking buchen."""
        return kitchen.update_pantry(_dump(items), stocktake_location)

    @mcp.tool(annotations=read)
    @guarded
    def find_recipes(
        query: Annotated[str | None, Field(description="Name, Stichwort oder Zutat")] = None,
        ingredients: Annotated[list[str] | None, Field(description="Soll diese Zutaten verwenden, z. B. ['Zucchini']")] = None,
        use_expiring: Annotated[bool, Field(description="Nur Rezepte, die bald Ablaufendes verwerten")] = False,
        max_minutes: int | None = None,
    ) -> dict[str, Any]:
        """Sucht gespeicherte Rezepte, passend zu Vorrat, bald Ablaufendem, Angeboten und Haushaltsregeln:
        'Was mache ich mit Zucchini?', 'Was Schnelles für heute?'. Findet sich nichts, liefert es Hinweise (Ablaufendes,
        Regeln), damit du selbst Gerichte vorschlagen kannst. Gefällt dem Nutzer ein Vorschlag: save_recipe oder plan_meals."""
        return kitchen.find_recipes(query, ingredients, use_expiring, max_minutes)

    @mcp.tool(annotations=write)
    @guarded
    def save_recipe(recipe: Recipe) -> dict[str, Any]:
        """Speichert ein Rezept dauerhaft: 'Speicher dir das Rezept', oder ein Gericht, das dem Nutzer gefallen hat.
        Zutaten mit Mengen für `servings` Portionen angeben; der Server ordnet Zutaten zu, rechnet in Grundeinheiten
        um und fragt bei Mehrdeutigem nach (z. B. Sahne → Schlagsahne oder Kochsahne). Gleicher Name ersetzt das Rezept."""
        return kitchen.save_recipe(_dump(recipe))

    @mcp.tool(annotations=write)
    @guarded
    def manage_household(
        members: Annotated[list[Member] | None, Field(description="Personen im Haushalt")] = None,
        restrictions: Annotated[list[Restriction] | None, Field(description="Allergien, Ernährungsform, Ausschlüsse – harte Regeln")] = None,
        nutrition_targets: Annotated[list[Target] | None, Field(description="Tagesziele, z. B. protein min 140")] = None,
        stores: Annotated[list[StorePref] | None, Field(description="Läden, in denen eingekauft wird oder nicht")] = None,
        preferences: Annotated[list[Preference] | None, Field(description="Weiche Vorlieben/Abneigungen")] = None,
        weekly_budget_eur: float | None = None,
        max_stores_per_trip: int | None = None,
        default_meal_types: Annotated[list[str] | None, Field(description="Welche Mahlzeiten normalerweise geplant werden")] = None,
        stores_answered: Annotated[bool | None, Field(description="true, wenn der Nutzer keine Läden festlegen will")] = None,
    ) -> dict[str, Any]:
        """Haushaltsprofil lesen oder ändern: 'Ich vertrage keine Laktose', 'Wir sind zu zweit', 'kein Aldi mehr',
        'Ich will 150 g Protein am Tag', 'Ich mag keine Pilze', 'Budget 70 € pro Woche'. Ohne Argumente: Profil anzeigen.
        Allergien und Ausschlüsse prüft der Server bei jedem Plan."""
        return kitchen.manage_household({k: v for k, v in {
            "members": _dump(members), "restrictions": _dump(restrictions), "nutrition_targets": _dump(nutrition_targets),
            "stores": _dump(stores), "preferences": _dump(preferences), "weekly_budget_eur": weekly_budget_eur,
            "max_stores_per_trip": max_stores_per_trip, "default_meal_types": default_meal_types,
            "stores_answered": stores_answered}.items() if v is not None})

    @mcp.tool(annotations=read)
    @guarded
    def find_offers(
        query: Annotated[str | None, Field(description="Produkt/Kategorie, z. B. 'Hähnchen', 'Skyr', 'Fisch'")] = None,
        stores: list[str] | None = None,
        min_protein_g_per_100g: float | None = None,
        max_price_eur: float | None = None,
        for_shopping_list: Annotated[bool, Field(description="Nur Angebote zu Posten auf der Einkaufsliste")] = False,
        sort_by: Literal["protein_per_euro", "price", "protein", "expiry"] = "protein_per_euro",
        limit: Annotated[int, Field(ge=1, le=50)] = 12,
    ) -> dict[str, Any]:
        """Durchsucht aktuelle Supermarkt-Angebote (Muskelmarkt, täglich aktualisiert): 'Was ist diese Woche günstig?',
        'Proteinreiche Angebote bei Lidl'. Beim Planen fließen Angebote automatisch ein – dafür nicht nötig."""
        return kitchen.find_offers(query, stores, min_protein_g_per_100g, max_price_eur, for_shopping_list, sort_by, limit)

    @mcp.tool(annotations=read)
    @guarded
    def get_insights(
        topic: Literal["overview", "spending", "prices", "cooking", "nutrition", "waste"] = "overview",
        days: Annotated[int, Field(ge=1, le=366)] = 30,
        ingredient: Annotated[str | None, Field(description="Für Preisverlauf einer Zutat")] = None,
    ) -> dict[str, Any]:
        """Auswertungen: 'Was habe ich diesen Monat ausgegeben?', 'Was koche ich am häufigsten?', 'Wie viel werfe ich weg?',
        'Wo ist Skyr am günstigsten?'."""
        return kitchen.get_insights(topic, days, ingredient)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False))
    @guarded
    def undo_last_action() -> dict[str, Any]:
        """Macht die letzte gespeicherte Änderung rückgängig: 'Mach das rückgängig', 'Das war falsch'. Wirkt auf das
        jeweils letzte Kommando, mehrfach aufrufbar; das Ergebnis nennt, was zurückgenommen wurde. Wurde die gemeinte
        Änderung gar nicht gespeichert, nicht aufrufen, sondern das kurz sagen."""
        return kitchen.undo_last_action()

    @mcp.prompt(name="woche_planen", title="Woche planen")
    def woche_planen() -> str:
        return "Plane mir die nächste Woche (Abendessen). Nutze zuerst, was weg muss, und achte auf Angebote."

    @mcp.prompt(name="einkauf_erfassen", title="Einkauf erfassen")
    def einkauf_erfassen() -> str:
        return "Ich war einkaufen. Frag mich kurz, ob alles von der Liste da war, und buche es."

    @mcp.prompt(name="was_koche_ich_heute", title="Was koche ich heute?")
    def was_koche_ich_heute() -> str:
        return "Was koche ich heute? Schau in den Plan und den Vorrat."

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request):  # type: ignore[no-untyped-def]
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": True, "api_version": API_VERSION})

    return mcp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kitchen Agent MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("KITCHEN_TRANSPORT", "stdio"))
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("KITCHEN_DB", DEFAULT_DB)))
    parser.add_argument("--offers", type=Path, default=Path(os.environ["KITCHEN_OFFERS_FILE"]) if os.environ.get("KITCHEN_OFFERS_FILE") else None,
                        help="Optional Muskelmarkt export to seed the offer cache")
    parser.add_argument("--no-refresh", action="store_true", default=os.environ.get("KITCHEN_NO_REFRESH") == "1",
                        help="Do not fetch offers from the web (tests)")
    parser.add_argument("--host", default=os.environ.get("KITCHEN_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("KITCHEN_PORT", "8000")))
    parser.add_argument("--inventory-db", type=Path, help=argparse.SUPPRESS)  # accepted for the old command line
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("KITCHEN_LOG", "INFO"), stream=sys.stderr,
                        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
    kitchen = Kitchen(args.db, offers_file=args.offers, auto_refresh=not args.no_refresh)
    server = build_server(kitchen)
    if args.transport == "stdio":
        server.run("stdio")
        return 0
    import uvicorn
    app = server.streamable_http_app(host=args.host)
    token = os.environ.get("KITCHEN_TOKEN")
    if token:
        app = _require_token(app, token)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _require_token(app: Any, token: str) -> Any:
    """Secret gate for HTTP mode; /healthz stays open for uptime checks.

    Clients that can send headers use `Authorization: Bearer <token>`. ChatGPT and claude.ai connectors only offer OAuth
    or no authentication, so the same secret also works as a path prefix: https://host/<token>/mcp ("No authentication").
    """
    prefix = f"/{token}"

    async def gated(scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] == "http" and scope.get("path") != "/healthz":
            path = scope.get("path") or ""
            headers = dict(scope.get("headers") or [])
            if path == prefix or path.startswith(prefix + "/"):
                inner = path[len(prefix):] or "/"
                scope = dict(scope, path=inner, raw_path=inner.encode())
            elif headers.get(b"authorization", b"").decode() != f"Bearer {token}":
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await app(scope, receive, send)
    return gated


if __name__ == "__main__":
    raise SystemExit(main())
