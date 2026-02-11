#!/usr/bin/env python3
"""Interaktywny skrypt do przeglądu i akceptacji faktur KSeF."""

import os
import sys
import glob
import subprocess
from datetime import datetime, date
from lxml import etree

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INVOICES_DIR = os.path.join(SCRIPT_DIR, "faktury")
OUTPUT_BASE_DIR = os.path.join(SCRIPT_DIR, "faktury_potwierdzone")


def suggest_month():
    """Sugeruje miesiąc do przeglądu: poprzedni jeśli dzień <= 15, bieżący jeśli > 15."""
    today = date.today()
    if today.day <= 15:
        # Poprzedni miesiąc
        if today.month == 1:
            return f"{today.year - 1}-12"
        return f"{today.year}-{today.month - 1:02d}"
    return f"{today.year}-{today.month:02d}"


def choose_month():
    """Pozwala użytkownikowi wybrać miesiąc."""
    suggested = suggest_month()
    user_input = input(f"Miesiąc do przeglądu [{suggested}]: ").strip()
    if not user_input:
        return suggested
    # Walidacja formatu YYYY-MM
    try:
        datetime.strptime(user_input, "%Y-%m")
    except ValueError:
        print("Nieprawidłowy format. Oczekiwany: YYYY-MM")
        sys.exit(1)
    return user_input


def fetch_new_invoices():
    """Pobiera zaległe faktury przez fetch_invoices.py."""
    print("\nPobieranie zaległych faktur z KSeF...")
    result = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, "fetch_invoices.py"), "--format", "text"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(result.stdout.strip())
    else:
        print(f"Uwaga: pobieranie nie powiodło się: {result.stderr.strip()}")


def parse_invoice(xml_path):
    """Parsuje fakturę XML i zwraca słownik z kluczowymi danymi."""
    tree = etree.parse(xml_path)
    root = tree.getroot()

    # Detekcja namespace
    ns = root.tag.split("}")[0].lstrip("{") if "}" in root.tag else ""

    def xpath_local(element, local_path):
        """XPath namespace-agnostic — szuka po local-name()."""
        parts = local_path.split("/")
        expr = "/".join(f"*[local-name()='{p}']" for p in parts)
        return element.xpath(expr)

    def text(element, local_path, default=""):
        nodes = xpath_local(element, local_path)
        if nodes and nodes[0].text:
            return nodes[0].text.strip()
        return default

    # Nazwa kontrahenta (Podmiot1)
    nazwa = text(root, "Podmiot1/DaneIdentyfikacyjne/Nazwa")

    # Dane faktury
    fa_nodes = xpath_local(root, "Fa")
    if not fa_nodes:
        return None
    fa = fa_nodes[0]

    data_faktury = text(fa, "P_1")
    numer_faktury = text(fa, "P_2")
    kwota_brutto = text(fa, "P_15")
    waluta = text(fa, "KodWaluty", "PLN")

    # Pozycje
    pozycje = []
    for wiersz in xpath_local(fa, "FaWiersz"):
        opis = text(wiersz, "P_7")
        kwota_netto = text(wiersz, "P_11") or text(wiersz, "P_11A")
        pozycje.append({"opis": opis, "kwota_netto": kwota_netto})

    return {
        "path": xml_path,
        "nazwa": nazwa,
        "data": data_faktury,
        "numer": numer_faktury,
        "kwota_brutto": kwota_brutto,
        "waluta": waluta,
        "pozycje": pozycje,
    }


def load_invoices(year_month):
    """Ładuje i filtruje faktury po wybranym miesiącu."""
    xml_files = glob.glob(os.path.join(INVOICES_DIR, "*.xml"))
    if not xml_files:
        print("Brak faktur XML w folderze faktury/")
        sys.exit(0)

    invoices = []
    for xml_path in sorted(xml_files):
        inv = parse_invoice(xml_path)
        if inv is None:
            continue
        # Filtrowanie po miesiącu (P_1 = YYYY-MM-DD)
        if inv["data"].startswith(year_month):
            invoices.append(inv)

    return invoices


def review_invoices(invoices):
    """Interaktywny przegląd faktur. Zwraca (zaakceptowane, odrzucone)."""
    accepted = []
    rejected = []
    total = len(invoices)

    for i, inv in enumerate(invoices, 1):
        print(f"\n[{i}/{total}] Faktura {inv['data']} — {inv['nazwa']}")
        print(f"  Numer: {inv['numer']}")
        if inv["pozycje"]:
            print("  Pozycje:")
            for j, poz in enumerate(inv["pozycje"], 1):
                print(f"    {j}. {poz['opis']} — {poz['kwota_netto']} {inv['waluta']}")
        print(f"  Kwota brutto: {inv['kwota_brutto']} {inv['waluta']}")

        answer = input("  Akceptujesz? [T/n]: ").strip().lower()
        if answer in ("", "t", "tak", "y", "yes"):
            accepted.append(inv)
        else:
            rejected.append(inv)

    return accepted, rejected


def confirm_and_transform(accepted, rejected, year_month):
    """Podsumowuje wybory i generuje PDF-y dla zaakceptowanych faktur."""
    print(f"\n--- Podsumowanie ---")
    print(f"  Zaakceptowano: {len(accepted)}")
    print(f"  Odrzucono:     {len(rejected)}")

    if not accepted:
        print("Brak zaakceptowanych faktur. Kończę.")
        return

    answer = input("\nZatwierdzić i wygenerować PDF-y? [T/n]: ").strip().lower()
    if answer not in ("", "t", "tak", "y", "yes"):
        print("Anulowano.")
        return

    output_dir = os.path.join(OUTPUT_BASE_DIR, year_month)
    os.makedirs(output_dir, exist_ok=True)

    from transform_invoices import transform_to_pdf

    print(f"\nGenerowanie PDF-ów do {output_dir}/")
    success = 0
    for inv in accepted:
        filename = os.path.basename(inv["path"])
        try:
            pdf_path = transform_to_pdf(inv["path"], output_dir)
            print(f"  OK: {filename} -> {os.path.basename(pdf_path)}")
            success += 1
        except Exception as e:
            print(f"  BŁĄD: {filename} — {e}")

    print(f"\nWygenerowano {success}/{len(accepted)} PDF-ów w {output_dir}/")


def main():
    year_month = choose_month()
    fetch_new_invoices()

    print(f"\nSzukam faktur za {year_month}...")
    invoices = load_invoices(year_month)

    if not invoices:
        print(f"Brak faktur za {year_month}.")
        sys.exit(0)

    print(f"Znaleziono {len(invoices)} faktur do przeglądu.")

    accepted, rejected = review_invoices(invoices)
    confirm_and_transform(accepted, rejected, year_month)


if __name__ == "__main__":
    main()
