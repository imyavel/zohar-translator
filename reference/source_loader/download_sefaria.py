"""
Скрипт загрузки полного корпуса Зоар с комментарием Сулам из Sefaria.org
Проект: Зоар-Сулам → Русский → Wikibooks
"""

import requests
import json
import os
import time
import sys
from pathlib import Path
from datetime import datetime

# ─── Конфигурация ───────────────────────────────────────────────────────────

BASE_URL = "https://www.sefaria.org/api"

# Куда писать загруженные секции и итоговый каталог:
#   1) SEFARIA_OUT, если задан явно;
#   2) $HEB_ROOT/Source, если задан HEB_ROOT;
#   3) ./Source рядом со скриптом (для smoke-теста).
def _default_base_dir() -> Path:
    if os.environ.get("SEFARIA_OUT"):
        return Path(os.environ["SEFARIA_OUT"])
    if os.environ.get("HEB_ROOT"):
        return Path(os.environ["HEB_ROOT"]) / "Source"
    return Path(__file__).resolve().parent / "Source"

BASE_DIR = _default_base_dir()
DELAY = 1.0  # секунд между запросами (вежливость к серверу)
TIMEOUT = 30  # секунд таймаут на запрос

# Секции Sulam on Zohar (53 секции, 15711 параграфов)
SULAM_SECTIONS = [
    "Introduction",
    "Bereshit_I", "Bereshit_II", "Noach", "Lech_Lecha",
    "Vayera", "Chayei_Sara", "Toldot", "Vayetzei",
    "Vayishlach", "Vayeshev", "Miketz", "Vayigash", "Vayechi",
    "Shemot", "Vaera", "Bo", "Beshalach",
    "Yitro", "Mishpatim",
    "Terumah", "Sifra_DiTzniuta", "Tetzaveh", "Ki_Tisa",
    "Vayakhel", "Pekudei",
    "Vayikra", "Tzav", "Shmini", "Tazria", "Metzora",
    "Achrei_Mot", "Kedoshim", "Emor", "Behar", "Bechukotai",
    "Bamidbar", "Nasso", "Idra_Rabba", "Beha'alotcha",
    "Sh'lach", "Korach", "Chukat", "Balak",
    "Pinchas", "Matot",
    "Vaetchanan", "Eikev", "Shoftim", "Ki_Teitzei",
    "Vayeilech", "Ha'Azinu", "Idra_Zuta",
]

# Секции Zohar (51 секция, включая Addenda)
ZOHAR_SECTIONS = [
    "Introduction",
    "Bereshit", "Noach", "Lech_Lecha",
    "Vayera", "Chayei_Sara", "Toldot", "Vayetzei",
    "Vayishlach", "Vayeshev", "Miketz", "Vayigash", "Vayechi",
    "Shemot", "Vaera", "Bo", "Beshalach",
    "Yitro", "Mishpatim",
    "Terumah", "Sifra_DiTzniuta", "Tetzaveh", "Ki_Tisa",
    "Vayakhel", "Pekudei",
    "Vayikra", "Tzav", "Shmini", "Tazria", "Metzora",
    "Achrei_Mot", "Kedoshim", "Emor", "Behar", "Bechukotai",
    "Bamidbar", "Nasso", "Idra_Rabba", "Beha'alotcha",
    "Sh'lach", "Korach", "Chukat", "Balak",
    "Pinchas", "Matot",
    "Vaetchanan", "Eikev", "Shoftim", "Ki_Teitzei",
    "Vayeilech", "Ha'Azinu", "Idra_Zuta",
    "Addenda_Volume_I", "Addenda_Volume_II", "Addenda_Volume_III",
]

# Секции Zohar Chadash
ZOHAR_CHADASH_SECTIONS = [
    "Bereshit", "Noach", "Lech_Lecha", "Vayera", "Toldot",
    "Vayetzei", "Vayeshev", "Beshalach", "Yitro",
    "Terumah", "Ki_Tisa", "Tzav", "Achrei_Mot",
    "Behar", "Nasso", "Chukat", "Balak", "Matot",
    "Vaetchanan", "Ki_Teitzei", "Ki_Tavo",
    "Shir_HaShirim", "Midrash_Rut",
    "Midrash_HaNe'elam_Al_Eichah",
    "Tikkunim_Mizohar_Chadash",
]

# ─── Функции ────────────────────────────────────────────────────────────────

def download_section(work_ref: str, section: str, out_dir: Path) -> dict | None:
    """Скачивает одну секцию через Sefaria API и сохраняет JSON."""
    ref = f"{work_ref},_{section}" if "," not in work_ref else f"{work_ref}_{section}"
    url = f"{BASE_URL}/texts/{ref}?context=0&pad=0"
    out_file = out_dir / f"{section}.json"

    if out_file.exists():
        print(f"  [SKIP] {section} — уже скачан")
        with open(out_file, "r", encoding="utf-8") as f:
            return json.load(f)

    try:
        resp = requests.get(url, timeout=TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            # Проверяем наличие текста
            he_text = data.get("he", [])
            if isinstance(he_text, list):
                # Подсчёт параграфов (может быть вложенный список)
                count = count_paragraphs(he_text)
            else:
                count = 0
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"  [OK]   {section} — {count} параграфов")
            return data
        else:
            print(f"  [ERR]  {section} — HTTP {resp.status_code}")
            return None
    except Exception as e:
        print(f"  [ERR]  {section} — {e}")
        return None


def download_tikkunei_zohar(out_dir: Path) -> list:
    """Tikkunei Zohar использует нотацию Daf (страниц), скачиваем по-другому."""
    results = []
    # Сначала получаем shape для определения структуры
    shape_url = f"{BASE_URL}/shape/Tikkunei_Zohar"
    try:
        resp = requests.get(shape_url, timeout=TIMEOUT)
        if resp.status_code == 200:
            shape = resp.json()
            with open(out_dir / "_shape.json", "w", encoding="utf-8") as f:
                json.dump(shape, f, ensure_ascii=False, indent=2)
            print(f"  [INFO] Tikkunei Zohar shape: {len(shape)} элементов")
        else:
            print(f"  [WARN] Не удалось получить shape: HTTP {resp.status_code}")
    except Exception as e:
        print(f"  [WARN] Shape error: {e}")

    # Скачиваем по дафим (страницам)
    daf_names = []
    for i in range(1, 149):  # дафим 1-148
        for side in ["a", "b"]:
            daf_names.append(f"{i}{side}")

    for daf in daf_names:
        out_file = out_dir / f"daf_{daf}.json"
        if out_file.exists():
            print(f"  [SKIP] Daf {daf}")
            continue

        url = f"{BASE_URL}/texts/Tikkunei_Zohar.{daf}?context=0&pad=0"
        try:
            resp = requests.get(url, timeout=TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                he_text = data.get("he", [])
                count = count_paragraphs(he_text) if isinstance(he_text, list) else 0
                if count > 0:
                    with open(out_file, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    print(f"  [OK]   Daf {daf} — {count} параграфов")
                    results.append({"daf": daf, "paragraphs": count})
                else:
                    print(f"  [EMPTY] Daf {daf}")
            elif resp.status_code == 404:
                pass  # нормально — не все дафим существуют
            else:
                print(f"  [ERR]  Daf {daf} — HTTP {resp.status_code}")
        except Exception as e:
            print(f"  [ERR]  Daf {daf} — {e}")
        time.sleep(DELAY * 0.5)  # чуть быстрее, т.к. много мелких запросов

    return results


def count_paragraphs(data) -> int:
    """Рекурсивный подсчёт текстовых элементов (параграфов)."""
    if isinstance(data, str):
        return 1 if data.strip() else 0
    if isinstance(data, list):
        return sum(count_paragraphs(item) for item in data)
    return 0


def download_work(work_name: str, work_ref: str, sections: list, out_dir: Path) -> dict:
    """Скачивает все секции одного произведения."""
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n{'='*60}")
    print(f"Загрузка: {work_name} ({len(sections)} секций)")
    print(f"Директория: {out_dir}")
    print(f"{'='*60}")

    # Сначала скачиваем shape (структуру)
    shape_url = f"{BASE_URL}/shape/{work_ref.replace(',', '')}"
    try:
        resp = requests.get(shape_url, timeout=TIMEOUT)
        if resp.status_code == 200:
            with open(out_dir / "_shape.json", "w", encoding="utf-8") as f:
                json.dump(resp.json(), f, ensure_ascii=False, indent=2)
            print(f"  [INFO] Shape сохранён")
    except:
        pass

    # Скачиваем index (метаданные)
    index_url = f"{BASE_URL}/v2/index/{work_ref.replace(',', '_')}"
    try:
        resp = requests.get(index_url, timeout=TIMEOUT)
        if resp.status_code == 200:
            with open(out_dir / "_index.json", "w", encoding="utf-8") as f:
                json.dump(resp.json(), f, ensure_ascii=False, indent=2)
            print(f"  [INFO] Index сохранён")
    except:
        pass

    time.sleep(DELAY)

    catalog_entry = {
        "work": work_name,
        "ref": work_ref,
        "sections": [],
        "total_paragraphs": 0,
    }

    ok_count = 0
    err_count = 0

    for i, section in enumerate(sections):
        data = download_section(work_ref, section, out_dir)
        if data:
            he_text = data.get("he", [])
            count = count_paragraphs(he_text) if isinstance(he_text, list) else 0
            catalog_entry["sections"].append({
                "section": section,
                "paragraphs": count,
                "ref": data.get("ref", ""),
            })
            catalog_entry["total_paragraphs"] += count
            ok_count += 1
        else:
            catalog_entry["sections"].append({
                "section": section,
                "paragraphs": 0,
                "error": True,
            })
            err_count += 1

        # Прогресс
        pct = (i + 1) / len(sections) * 100
        print(f"  Прогресс: {i+1}/{len(sections)} ({pct:.0f}%)")
        time.sleep(DELAY)

    print(f"\n  Итого {work_name}: {ok_count} OK, {err_count} ошибок, "
          f"{catalog_entry['total_paragraphs']} параграфов")

    return catalog_entry


def main():
    print(f"Начало загрузки: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Директория: {BASE_DIR}")

    catalog = {
        "project": "Зоар-Сулам → Русский → Wikibooks",
        "source": "sefaria.org",
        "download_date": datetime.now().isoformat(),
        "works": [],
    }

    # 1. Sulam on Zohar (основной — комментарий)
    entry = download_work(
        "Sulam on Zohar",
        "Sulam_on_Zohar",
        SULAM_SECTIONS,
        BASE_DIR / "Sulam_on_Zohar",
    )
    catalog["works"].append(entry)

    # 2. Zohar (оригинальный арамейский текст)
    entry = download_work(
        "Zohar",
        "Zohar",
        ZOHAR_SECTIONS,
        BASE_DIR / "Zohar",
    )
    catalog["works"].append(entry)

    # 3. Zohar Chadash
    entry = download_work(
        "Zohar Chadash",
        "Zohar_Chadash",
        ZOHAR_CHADASH_SECTIONS,
        BASE_DIR / "Zohar_Chadash",
    )
    catalog["works"].append(entry)

    # 4. Tikkunei Zohar (особая структура — по дафим)
    print(f"\n{'='*60}")
    print(f"Загрузка: Tikkunei Zohar (по дафим/страницам)")
    print(f"{'='*60}")
    tk_dir = BASE_DIR / "Tikkunei_Zohar"
    tk_dir.mkdir(parents=True, exist_ok=True)
    tk_results = download_tikkunei_zohar(tk_dir)
    tk_total = sum(r["paragraphs"] for r in tk_results)
    catalog["works"].append({
        "work": "Tikkunei Zohar",
        "ref": "Tikkunei_Zohar",
        "sections": tk_results,
        "total_paragraphs": tk_total,
        "total_dafim": len(tk_results),
    })

    # Сохраняем каталог
    total_all = sum(w["total_paragraphs"] for w in catalog["works"])
    catalog["total_paragraphs_all_works"] = total_all

    catalog_path = BASE_DIR / "catalog.json"
    with open(catalog_path, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"ЗАГРУЗКА ЗАВЕРШЕНА")
    print(f"{'='*60}")
    for w in catalog["works"]:
        scount = len(w.get("sections", []))
        pcount = w["total_paragraphs"]
        print(f"  {w['work']}: {scount} секций, {pcount} параграфов")
    print(f"  ВСЕГО: {total_all} параграфов")
    print(f"  Каталог: {catalog_path}")
    print(f"Завершено: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()


# ─── Версия 001: Первоначальный скрипт загрузки Зоар из Sefaria API ────────
