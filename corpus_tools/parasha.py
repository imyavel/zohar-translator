"""Pure-Python computation of weekly Torah portion (parasha) for diaspora.

Hillel II / Reingold-Dershowitz formulation of the Hebrew calendar; standard
keviyah-based parasha cycle table for diaspora.

Public API:
    build_table(start_year, end_year) -> dict[iso_date, parasha_key]
        Maps each Saturday in [start_year, end_year) Gregorian to the parasha
        key (or "FESTIVAL" if a yom tov / chol hamoed Shabbat).
"""
from __future__ import annotations

from datetime import date, timedelta

# --- Hebrew calendar core (Reingold-Dershowitz formulation) ---

HEBREW_EPOCH = -1373428  # R.D. for 1 Tishri AM 1 (proleptic Gregorian)


def is_hebrew_leap(year: int) -> bool:
    return (7 * year + 1) % 19 < 7


def hebrew_calendar_elapsed_days(year: int) -> int:
    """Days from Hebrew epoch to 1 Tishri of `year`."""
    months_elapsed = (
        235 * ((year - 1) // 19)
        + 12 * ((year - 1) % 19)
        + (7 * ((year - 1) % 19) + 1) // 19
    )
    parts_elapsed = 204 + 793 * (months_elapsed % 1080)
    hours_elapsed = (
        5
        + 12 * months_elapsed
        + 793 * (months_elapsed // 1080)
        + parts_elapsed // 1080
    )
    conjunction_day = 1 + 29 * months_elapsed + hours_elapsed // 24
    conjunction_parts = 1080 * (hours_elapsed % 24) + (parts_elapsed % 1080)

    if (
        conjunction_parts >= 19440
        or (
            conjunction_day % 7 == 2
            and conjunction_parts >= 9924
            and not is_hebrew_leap(year)
        )
        or (
            conjunction_day % 7 == 1
            and conjunction_parts >= 16789
            and is_hebrew_leap(year - 1)
        )
    ):
        alternative_day = conjunction_day + 1
    else:
        alternative_day = conjunction_day

    if alternative_day % 7 in (0, 3, 5):
        return alternative_day + 1
    return alternative_day


def hebrew_year_length(year: int) -> int:
    return hebrew_calendar_elapsed_days(year + 1) - hebrew_calendar_elapsed_days(year)


def hebrew_new_year(year: int) -> date:
    rd = HEBREW_EPOCH + hebrew_calendar_elapsed_days(year)
    return date.fromordinal(rd)


def gregorian_to_hebrew_year(g: date) -> int:
    """Hebrew year containing the Gregorian date `g`."""
    # Approximate: Hebrew year starts in Sept/Oct.
    approx = g.year + 3761
    # Adjust if g is before that year's RH
    if g < hebrew_new_year(approx):
        approx -= 1
    elif g >= hebrew_new_year(approx + 1):
        approx += 1
    return approx


def hebrew_to_gregorian(h_year: int, h_month: int, h_day: int) -> date:
    """h_month: 1=Nisan, 2=Iyar, ..., 7=Tishri, ..., 12=Elul (or 13=Adar II in leap).
    For our needs we use Tishri-relative numbering: 1=Tishri, 2=Heshvan, etc.
    """
    # We'll use Tishri-based numbering: 1=Tishri, 2=Heshvan, 3=Kislev, 4=Tevet,
    # 5=Shevat, 6=Adar (or Adar I in leap), 7=Adar II (leap only)/Nisan,
    # 8=Iyar, 9=Sivan, 10=Tammuz, 11=Av, 12=Elul.
    #
    # In a non-leap year: 1=Tishri..12=Elul (Nisan = 7).
    # In a leap year: 1=Tishri..13=Elul (Nisan = 8, Adar I = 6, Adar II = 7).
    rd = HEBREW_EPOCH + hebrew_calendar_elapsed_days(h_year)
    ny_len = hebrew_year_length(h_year)
    leap = is_hebrew_leap(h_year)
    # Determine days-in-month sequence starting from Tishri.
    # Length of year determines Heshvan (29 or 30) and Kislev (29 or 30).
    # 353/383: short — Heshvan=29, Kislev=29
    # 354/384: regular — Heshvan=29, Kislev=30
    # 355/385: full — Heshvan=30, Kislev=30
    if ny_len in (353, 383):
        cheshvan, kislev = 29, 29
    elif ny_len in (354, 384):
        cheshvan, kislev = 29, 30
    else:  # 355 or 385
        cheshvan, kislev = 30, 30
    if leap:
        # Tishri Heshvan Kislev Tevet Shevat AdarI AdarII Nisan Iyar Sivan Tammuz Av Elul
        days = [30, cheshvan, kislev, 29, 30, 30, 29, 30, 29, 30, 29, 30, 29]
    else:
        # Tishri Heshvan Kislev Tevet Shevat Adar Nisan Iyar Sivan Tammuz Av Elul
        days = [30, cheshvan, kislev, 29, 30, 29, 30, 29, 30, 29, 30, 29]
    # Sum days in months before h_month
    rd += sum(days[: h_month - 1]) + (h_day - 1)
    return date.fromordinal(rd)


# --- Parasha cycle ---

# 54 parashot in canonical order (matching catalog `chapter` keys where possible).
PARASHOT = [
    "Bereshit", "Noach", "Lech_Lecha", "Vayera", "Chayei_Sara", "Toldot",
    "Vayetzei", "Vayishlach", "Vayeshev", "Miketz", "Vayigash", "Vayechi",
    "Shemot", "Vaera", "Bo", "Beshalach", "Yitro", "Mishpatim",
    "Terumah", "Tetzaveh", "Ki_Tisa", "Vayakhel", "Pekudei",
    "Vayikra", "Tzav", "Shmini", "Tazria", "Metzora",
    "Achrei_Mot", "Kedoshim", "Emor", "Behar", "Bechukotai",
    "Bamidbar", "Nasso", "Beha'alotcha", "Sh'lach", "Korach",
    "Chukat", "Balak", "Pinchas", "Matot", "Mas'ei",
    "Devarim", "Vaetchanan", "Eikev", "Re'eh", "Shoftim",
    "Ki_Teitzei", "Ki_Tavo", "Nitzavim", "Vayeilech", "Ha'Azinu",
    "V'Zot_HaBerachah",
]

# Indices of first parasha in each combinable pair.
PAIR_FIRST_IDX = {
    "VP": 21,  # Vayakhel + Pekudei
    "TM": 26,  # Tazria + Metzora
    "AK": 28,  # Achrei_Mot + Kedoshim
    "BB": 31,  # Behar + Bechukotai
    "CB": 38,  # Chukat + Balak
    "MM": 41,  # Matot + Mas'ei
    "NV": 50,  # Nitzavim + Vayelech
}

# Keviyah → list of doubled pairs (diaspora).
# Key = (RH-day-of-week-1=Sun..7=Sat, year-type 'D'/'R'/'F', leap-bool).
KEVIYAH_DOUBLES_DIASPORA = {
    # Non-leap (12-month):
    (2, "D", False): ["VP", "TM", "AK", "BB", "CB", "MM", "NV"],   # BaChaG
    (2, "F", False): ["VP", "TM", "AK", "BB", "MM", "NV"],          # BaShaH
    (3, "R", False): ["VP", "TM", "AK", "BB", "MM", "NV"],          # GaKaH
    (5, "R", False): ["VP", "TM", "AK", "BB", "CB", "MM", "NV"],    # HaKaZ
    (5, "F", False): ["VP", "TM", "AK", "BB", "MM", "NV"],          # HaShA
    (7, "D", False): ["VP", "TM", "AK", "BB", "CB", "MM", "NV"],    # ZaChA
    (7, "F", False): ["VP", "TM", "AK", "BB", "MM", "NV"],          # ZaShaG
    # Leap (13-month):
    (2, "D", True):  ["MM"],                            # BaChaH
    (2, "F", True):  ["CB", "MM"],                      # BaShaZ
    (3, "R", True):  ["CB", "MM", "NV"],                # GaKaZ
    (5, "D", True):  ["MM"],                            # HaChA
    (5, "F", True):  ["NV"],                            # HaShaG
    (7, "D", True):  ["CB", "MM", "NV"],                # ZaChaG
    (7, "F", True):  ["MM"],                            # ZaShaH
}


def year_type_letter(year_length: int) -> str:
    if year_length in (353, 383):
        return "D"
    if year_length in (354, 384):
        return "R"
    return "F"


def keviyah(h_year: int) -> tuple[int, str, bool]:
    """Returns (RH day-of-week 1=Sun..7=Sat, year-type letter, is_leap)."""
    rh = hebrew_new_year(h_year)
    # Python weekday: Mon=0..Sun=6. We want 1=Sun..7=Sat.
    # date.isoweekday(): Mon=1..Sun=7. Convert: Sun=1, Mon=2, ..., Sat=7.
    dow = (rh.isoweekday() % 7) + 1
    return (dow, year_type_letter(hebrew_year_length(h_year)), is_hebrew_leap(h_year))


# --- Festival Shabbatot (diaspora): days when weekly parasha is NOT read. ---
# Returns list of (Hebrew month, Hebrew day) tuples that are festival days.
# Months use Tishri-based numbering: 1=Tishri..12=Elul (or 13 in leap).

def festival_days_diaspora(h_year: int) -> set[tuple[int, int]]:
    """Yom-tov + chol-hamoed days. If a Shabbat falls on any → no parasha read."""
    leap = is_hebrew_leap(h_year)
    nisan = 7 if not leap else 8
    sivan = 9 if not leap else 10
    days = set()
    # Rosh Hashana
    days.update([(1, 1), (1, 2)])
    # Yom Kippur
    days.add((1, 10))
    # Sukkot 1-2
    days.update([(1, 15), (1, 16)])
    # Chol Hamoed Sukkot 17-21
    for d in range(17, 22):
        days.add((1, d))
    # Shemini Atzeret + Simchat Torah
    days.update([(1, 22), (1, 23)])
    # Pesach: 15-16, 21-22
    days.update([(nisan, 15), (nisan, 16), (nisan, 21), (nisan, 22)])
    # Chol Hamoed Pesach: 17-20
    for d in range(17, 21):
        days.add((nisan, d))
    # Shavuot 6-7
    days.update([(sivan, 6), (sivan, 7)])
    return days


def shabbat_is_festival(shabbat: date, h_year_for_shabbat: int) -> bool:
    """True iff this Shabbat is a yom-tov/chol-hamoed day in diaspora."""
    rh = hebrew_new_year(h_year_for_shabbat)
    days_into_year = (shabbat - rh).days  # 0-based days from 1 Tishri
    # Walk months
    leap = is_hebrew_leap(h_year_for_shabbat)
    yl = hebrew_year_length(h_year_for_shabbat)
    if yl in (353, 383):
        cheshvan, kislev = 29, 29
    elif yl in (354, 384):
        cheshvan, kislev = 29, 30
    else:
        cheshvan, kislev = 30, 30
    if leap:
        month_lengths = [30, cheshvan, kislev, 29, 30, 30, 29, 30, 29, 30, 29, 30, 29]
    else:
        month_lengths = [30, cheshvan, kislev, 29, 30, 29, 30, 29, 30, 29, 30, 29]
    remaining = days_into_year
    h_month = 1
    for ml in month_lengths:
        if remaining < ml:
            break
        remaining -= ml
        h_month += 1
    h_day = remaining + 1
    return (h_month, h_day) in festival_days_diaspora(h_year_for_shabbat)


def shabbat_bereshit(h_year: int) -> date:
    """First Shabbat of the cycle for Hebrew year `h_year` —
    the Shabbat immediately after Simchat Torah (23 Tishri in diaspora)."""
    simchat_torah = hebrew_to_gregorian(h_year, 1, 23)
    # Find next Saturday strictly after Simchat Torah
    days_until_sat = (5 - simchat_torah.weekday()) % 7  # Mon=0..Sun=6; Sat=5
    if days_until_sat == 0:
        days_until_sat = 7  # next, not same
    return simchat_torah + timedelta(days=days_until_sat)


def cycle_for_year(h_year: int) -> list[tuple[date, str]]:
    """Returns list of (shabbat_date, parasha_key_or_FESTIVAL) for the entire
    reading cycle of Hebrew year h_year (from Shabbat Bereshit through the
    last Shabbat before Simchat Torah of h_year+1)."""
    start = shabbat_bereshit(h_year)
    end = shabbat_bereshit(h_year + 1)  # exclusive
    kv = keviyah(h_year)
    doubled_codes = KEVIYAH_DOUBLES_DIASPORA[kv]
    doubled_first_indices = {PAIR_FIRST_IDX[c] for c in doubled_codes}

    out: list[tuple[date, str]] = []
    pidx = 0
    s = start
    while s < end:
        # Determine which Hebrew year contains this Shabbat (could be h_year or h_year+1).
        h_for_s = gregorian_to_hebrew_year(s)
        if shabbat_is_festival(s, h_for_s):
            out.append((s, "FESTIVAL"))
        else:
            if pidx >= len(PARASHOT):
                out.append((s, "OVERFLOW"))
            elif pidx in doubled_first_indices:
                key = f"{PARASHOT[pidx]}+{PARASHOT[pidx + 1]}"
                out.append((s, key))
                pidx += 2
            else:
                out.append((s, PARASHOT[pidx]))
                pidx += 1
        s += timedelta(days=7)
    return out


def build_table(year_from: int, year_to: int) -> dict[str, str]:
    """Build {iso_date: parasha_key} for Saturdays in given Gregorian span.

    Covers reading cycles of Hebrew years that overlap this span.
    """
    table: dict[str, str] = {}
    # Hebrew years involved
    hy_start = gregorian_to_hebrew_year(date(year_from, 1, 1))
    hy_end = gregorian_to_hebrew_year(date(year_to, 12, 31))
    for hy in range(hy_start, hy_end + 1):
        for s, key in cycle_for_year(hy):
            if year_from <= s.year <= year_to:
                table[s.isoformat()] = key
    return table


# --- Self-test ---

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    # Anchor checks
    checks = [
        (5784, date(2023, 9, 16), "RH 5784 = 2023-09-16 (Sat)"),
        (5785, date(2024, 10, 3), "RH 5785 = 2024-10-03 (Thu)"),
        (5786, date(2025, 9, 23), "RH 5786 = 2025-09-23 (Tue)"),
        (5787, date(2026, 9, 12), "RH 5787 = 2026-09-12 (Sat)"),
    ]
    for hy, expected, label in checks:
        got = hebrew_new_year(hy)
        ok = "OK" if got == expected else "FAIL"
        print(f"  [{ok}] {label} → got {got}")
    print()
    print(f"is_leap 5784: {is_hebrew_leap(5784)} (expected True)")
    print(f"is_leap 5786: {is_hebrew_leap(5786)} (expected False)")
    print(f"keviyah 5786: {keviyah(5786)} (expected (3, 'R', False) → GaKaH)")
    print()
    # Show cycle for 5786
    print("Cycle 5786 (first 40 Shabbatot):")
    for s, key in cycle_for_year(5786)[:40]:
        print(f"  {s} {s.strftime('%a')} → {key}")
