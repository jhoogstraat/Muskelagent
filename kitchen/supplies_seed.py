"""Built-in household supplies: non-food that runs out and needs restocking.

Counted in packs (base_unit "pc", one pack = 1). price_cents is a typical German discounter price per pack (2026).
restock_days is how long one pack lasts a single person; per_person=False items (pets, babies, household-wide things)
are not scaled by household size. The server learns the real interval from purchases and what the user says.
"""

SUPPLY_SECTION = "Drogerie & Sonstiges"


def _s(name: str, synonyms: list[str], category: str, price_cents: int, restock_days: int, per_person: bool = True) -> dict:
    return {"name": name, "synonyms": synonyms, "category": category, "price_cents": price_cents,
            "restock_days": restock_days, "per_person": per_person}


SUPPLIES = [
    # Papier
    _s("Toilettenpapier", ["Klopapier", "WC-Papier", "Toilettenrollen", "Klorollen", "Klopapierrollen", "Toilet paper"], "Haushalt", 350, 40),
    _s("Küchenrolle", ["Küchenpapier", "Küchenrollen", "Haushaltsrolle", "Haushaltsrollen", "Küchentücher", "Zewa", "Paper towels"], "Haushalt", 250, 45),
    _s("Taschentücher", ["Papiertaschentücher", "Tempo", "Tempos", "Kosmetiktücher", "Tissues"], "Haushalt", 150, 30),
    _s("Servietten", ["Papierservietten"], "Haushalt", 150, 120, per_person=False),
    # Küche
    _s("Spülmittel", ["Handspülmittel", "Geschirrspülmittel", "Pril", "Dish soap"], "Küche", 120, 45),
    _s("Spülmaschinentabs", ["Geschirrspültabs", "Spültabs", "Tabs", "Spülmaschinen-Tabs", "Geschirrspülmaschinentabs",
                             "Spülmaschinentabletten", "Finish Tabs", "Dishwasher tabs"], "Küche", 550, 80),
    _s("Klarspüler", ["Glanzspüler"], "Küche", 150, 150, per_person=False),
    _s("Spülmaschinensalz", ["Regeneriersalz", "Spülsalz", "Geschirrspülsalz"], "Küche", 120, 150, per_person=False),
    _s("Spülschwämme", ["Spülschwamm", "Schwämme", "Schwamm", "Topfschwamm", "Topfschwämme", "Schwammtücher"], "Küche", 150, 45, per_person=False),
    _s("Müllbeutel", ["Mülltüten", "Müllsäcke", "Abfallbeutel", "Müllbeutel 30 l", "Bin bags"], "Küche", 200, 40),
    _s("Biomüllbeutel", ["Biobeutel", "Kompostbeutel"], "Küche", 250, 60),
    _s("Frischhaltefolie", ["Klarsichtfolie", "Cling film"], "Küche", 150, 120, per_person=False),
    _s("Alufolie", ["Aluminiumfolie", "Alu-Folie", "Aluminium foil"], "Küche", 180, 120, per_person=False),
    _s("Backpapier", ["Backpapierrolle", "Baking paper"], "Küche", 150, 90, per_person=False),
    _s("Gefrierbeutel", ["Gefriertüten", "Zipperbeutel", "Frühstücksbeutel", "Freezer bags"], "Küche", 150, 90, per_person=False),
    _s("Kaffeefilter", ["Filtertüten", "Kaffeefiltertüten"], "Küche", 150, 90),
    # Wäsche
    _s("Waschmittel", ["Vollwaschmittel", "Colorwaschmittel", "Flüssigwaschmittel", "Waschpulver", "Waschpods", "Waschmittelkapseln",
                       "Feinwaschmittel", "Laundry detergent"], "Wäsche", 500, 60),
    _s("Weichspüler", ["Fabric softener"], "Wäsche", 150, 90),
    _s("Fleckenentferner", ["Fleckenspray", "Gallseife"], "Wäsche", 250, 180, per_person=False),
    # Reinigung
    _s("Allzweckreiniger", ["Universalreiniger", "Haushaltsreiniger", "Putzmittel", "All-purpose cleaner"], "Reinigung", 150, 120, per_person=False),
    _s("WC-Reiniger", ["Toilettenreiniger", "WC Reiniger", "Kloreiniger", "WC-Gel"], "Reinigung", 150, 75, per_person=False),
    _s("WC-Steine", ["Klosteine", "WC-Stein", "Duftspüler"], "Reinigung", 200, 60, per_person=False),
    _s("Badreiniger", ["Badezimmerreiniger", "Kalkreiniger", "Badspray"], "Reinigung", 150, 120, per_person=False),
    _s("Glasreiniger", ["Fensterreiniger", "Glass cleaner"], "Reinigung", 150, 180, per_person=False),
    _s("Entkalker", ["Kaffeemaschinenentkalker", "Wasserkocherentkalker"], "Reinigung", 250, 120, per_person=False),
    _s("Putztücher", ["Mikrofasertücher", "Putzlappen", "Spültücher", "Wischtücher"], "Reinigung", 200, 120, per_person=False),
    # Körperpflege
    _s("Zahnpasta", ["Zahncreme", "Zahnpaste", "Toothpaste"], "Körperpflege", 100, 45),
    _s("Zahnbürste", ["Zahnbürsten", "Aufsteckbürsten", "Bürstenköpfe", "Ersatzbürsten", "Toothbrush"], "Körperpflege", 300, 90),
    _s("Zahnseide", ["Interdentalbürsten", "Zahnzwischenraumbürsten", "Floss"], "Körperpflege", 150, 60),
    _s("Duschgel", ["Duschbad", "Duschcreme", "Shower gel"], "Körperpflege", 120, 30),
    _s("Shampoo", ["Haarshampoo"], "Körperpflege", 180, 45),
    _s("Haarspülung", ["Spülung", "Conditioner"], "Körperpflege", 180, 60),
    _s("Deo", ["Deodorant", "Deospray", "Deoroller", "Antitranspirant", "Deostick"], "Körperpflege", 180, 40),
    _s("Flüssigseife", ["Seife", "Handseife", "Nachfüllseife", "Seifenstück", "Hand soap"], "Körperpflege", 120, 40),
    _s("Rasierklingen", ["Rasierer", "Ersatzklingen", "Einwegrasierer", "Razor blades"], "Körperpflege", 800, 60),
    _s("Rasierschaum", ["Rasiergel"], "Körperpflege", 180, 60),
    _s("Wattepads", ["Abschminkpads", "Kosmetikpads"], "Körperpflege", 100, 60),
    _s("Wattestäbchen", ["Ohrenstäbchen"], "Körperpflege", 100, 120),
    _s("Bodylotion", ["Körperlotion", "Körpermilch", "Handcreme"], "Körperpflege", 200, 60),
    _s("Damenhygiene", ["Tampons", "Binden", "Slipeinlagen", "Periodenprodukte"], "Körperpflege", 250, 30, per_person=False),
    _s("Feuchttücher", ["Feuchtes Toilettenpapier", "Feuchtpflegetücher", "Babytücher"], "Körperpflege", 150, 45),
    # Baby, Tier, Sonstiges
    _s("Windeln", ["Babywindeln", "Pampers", "Diapers"], "Baby", 1000, 10, per_person=False),
    _s("Katzenfutter", ["Katzennassfutter", "Katzentrockenfutter", "Cat food"], "Tier", 600, 14, per_person=False),
    _s("Katzenstreu", ["Klumpstreu", "Cat litter"], "Tier", 500, 21, per_person=False),
    _s("Hundefutter", ["Hundenassfutter", "Hundetrockenfutter", "Dog food"], "Tier", 1200, 21, per_person=False),
    _s("Batterien", ["Batterie", "AA-Batterien", "AAA-Batterien", "Mignon-Batterien", "Batteries"], "Haushalt", 400, 180, per_person=False),
    _s("Teelichter", ["Teelicht", "Kerzen", "Tea lights"], "Haushalt", 250, 120, per_person=False),
]

if __name__ == "__main__":
    labels: dict[str, str] = {}
    for entry in SUPPLIES:
        for label in [entry["name"], *entry["synonyms"]]:
            key = label.casefold()
            assert key not in labels, f"duplicate label {label!r} ({labels[key]} / {entry['name']})"
            labels[key] = entry["name"]
    print(f"OK: {len(SUPPLIES)} supplies, {len(labels)} labels")
