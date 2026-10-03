"""Chemistry model: species, the acetylation reaction, work-up and analytical readouts.

Deliberately simple. Readouts are derived from what is really in the sample,
so an agent cannot get a clean spectrum from a dirty product.
"""
import math

SPECIES = {
    # name: molar mass g/mol, density g/mL (liquids), state at room temperature
    "salicylic_acid":   {"mw": 138.12, "state": "solid"},
    "acetic_anhydride": {"mw": 102.09, "state": "liquid", "density": 1.08},
    "sulfuric_acid":    {"mw": 98.08,  "state": "liquid", "density": 1.84},
    "water":            {"mw": 18.02,  "state": "liquid", "density": 1.00},
    "ethanol":          {"mw": 46.07,  "state": "liquid", "density": 0.789},
    "acetic_acid":      {"mw": 60.05,  "state": "liquid", "density": 1.049},
    "aspirin":          {"mw": 180.16, "state": "solid"},
    "byproduct":        {"mw": 300.0,  "state": "solid"},
}
SOLIDS = {"salicylic_acid", "aspirin", "byproduct"}


def grams(name, mmol):
    return mmol * SPECIES[name]["mw"] / 1000


def mmol_from_g(name, g):
    return g * 1000 / SPECIES[name]["mw"]


def mmol_from_ml(name, ml):
    return mmol_from_g(name, ml * SPECIES[name]["density"])


def react(contents, temp_c, minutes, k_scale=1.0):
    """Acetylation of salicylic acid. First order in salicylic acid, needs acid catalyst."""
    sa, ac2o = contents.get("salicylic_acid", 0), contents.get("acetic_anhydride", 0)
    if sa <= 0 or ac2o <= 0 or temp_c < 40:
        return
    k = 0.25 * k_scale * 2 ** ((temp_c - 80) / 10)          # per minute at 80 C with catalyst
    if contents.get("sulfuric_acid", 0) <= 0:
        k *= 0.05
    done = min(sa * (1 - math.exp(-k * minutes)), ac2o)
    contents["salicylic_acid"] = sa - done
    contents["acetic_anhydride"] = ac2o - done
    contents["aspirin"] = contents.get("aspirin", 0) + done
    contents["acetic_acid"] = contents.get("acetic_acid", 0) + done
    if temp_c > 105:                                         # decomposition when overheated
        lost = contents["aspirin"] * min(0.02 * minutes, 0.5)
        contents["aspirin"] -= lost
        contents["byproduct"] = contents.get("byproduct", 0) + lost * 180.16 / 300.0


def quench(contents):
    """Water destroys leftover acetic anhydride."""
    ac2o = contents.pop("acetic_anhydride", 0)
    contents["acetic_acid"] = contents.get("acetic_acid", 0) + 2 * ac2o
    contents["water"] = max(contents.get("water", 0) - ac2o, 0)


def solid_mass(contents):
    return sum(grams(s, contents.get(s, 0)) for s in SOLIDS)


def purity(contents):
    total = solid_mass(contents)
    return grams("aspirin", contents.get("aspirin", 0)) / total if total else 0.0


def impurity_fractions(contents):
    total = solid_mass(contents) or 1
    return {s: grams(s, contents.get(s, 0)) / total for s in ("salicylic_acid", "byproduct")}


# --- analytical readouts ----------------------------------------------------

def tlc(contents, eluent):
    if "ethyl_acetate" not in eluent:
        return "Single streak at baseline; eluent too non-polar to separate."
    f = impurity_fractions(contents)
    spots = []
    if purity(contents) > 0.01:
        spots.append("Rf 0.40 (strong, co-spots with aspirin standard)")
    if f["salicylic_acid"] > 0.02:
        strength = "strong" if f["salicylic_acid"] > 0.10 else "faint"
        spots.append(f"Rf 0.55 ({strength}, co-spots with salicylic acid standard)")
    if f["byproduct"] > 0.02:
        spots.append("Rf 0.05 (baseline smear)")
    return "TLC plate under UV 254 nm: " + "; ".join(spots)


def ferric_chloride(contents):
    sa = impurity_fractions(contents)["salicylic_acid"]
    if sa > 0.05:
        return "Deep purple colour: phenol present (unreacted salicylic acid)."
    if sa > 0.01:
        return "Faint violet tint: trace phenol present."
    return "Solution stays yellow: no phenol detected."


def melting_point(contents, offset_c=0.0):
    impurity = 1 - purity(contents)
    onset = 135.0 - 60 * impurity + offset_c
    width = 1.0 + 30 * impurity
    return f"Melting range {onset:.1f} - {onset + width:.1f} C (literature for aspirin: 135 - 136 C)."


def ir(contents):
    peaks = ["2500-3300 cm-1 broad (carboxylic acid O-H)", "1750 cm-1 strong (ester C=O)",
             "1690 cm-1 strong (acid C=O)", "1605 cm-1 (aromatic C=C)", "1185 cm-1 (C-O)"]
    if impurity_fractions(contents)["salicylic_acid"] > 0.05:
        peaks.insert(0, "3230 cm-1 medium (phenolic O-H, salicylic acid)")
        peaks.append("1655 cm-1 (salicylic acid C=O)")
    return "IR (ATR): " + "; ".join(peaks)


def nmr(contents, exclude_regions=None):
    peaks = [(11.0, "br s, 1H, COOH"), (8.12, "dd, 1H"), (7.62, "td, 1H"), (7.35, "t, 1H"),
             (7.13, "d, 1H"), (2.36, "s, 3H, OCOCH3")]
    sa = impurity_fractions(contents)["salicylic_acid"]
    if sa > 0.02:
        rel = sa / max(1 - sa, 0.01)
        peaks += [(10.4, f"s, {rel:.2f}H, phenol OH (salicylic acid)"),
                  (7.90, f"dd, {rel:.2f}H (salicylic acid)"), (6.98, f"d, {rel:.2f}H (salicylic acid)"),
                  (6.93, f"t, {rel:.2f}H (salicylic acid)")]
    for lo, hi in exclude_regions or []:
        peaks = [p for p in peaks if not lo <= p[0] <= hi]
    peaks.sort(reverse=True)
    return "1H NMR (400 MHz, CDCl3): " + ", ".join(f"{d:.2f} ({m})" for d, m in peaks)


def ferric_salicylate_assay(contents):
    sa = impurity_fractions(contents)["salicylic_acid"]
    absorbance = 25 * sa
    return (f"UV-Vis, Fe(III)-salicylate complex at 530 nm: A = {absorbance:.3f} "
            f"(calibration: A = 0.25 per 1% salicylic acid).")
