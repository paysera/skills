#!/usr/bin/env python3
"""Telefono numerių pagalbinės funkcijos — E.164 formatavimas.

E.164 formatas: +[šalies-kodas][abonemento-numeris], be tarpų, be brūkšnelių.
Pavyzdžiai: +37061234567, +447911123456, +12025550173

Naudojimas:
    from phone_utils import format_e164, PhoneFormatError

    num = format_e164("+370 612-34567")   # "+37061234567"
    num = format_e164("0037061234567")    # "+37061234567"
    num = format_e164("61234567", default_country_code="370")   # "+37061234567"

Magistraliniai prefiksai (lietuviškas "8", vokiškas "0") NEŠALINAMI automatiškai —
pašalinkite juos patys prieš perduodami vietinį numerį.
"""

from __future__ import annotations

import re

# Tarptautiniu mastu priskirti šalių kodų prefiksai (ITU-T E.164, 2024).
# Surikiuoti nuo ilgiausio, kad prefikso paieška būtų nedviprasmiška.
# Šaltinis: https://www.itu.int/pub/T-SP-E.164D
VALID_COUNTRY_CODES: frozenset[str] = frozenset(
    [
        # 1 skaitmuo
        "1",   # Šiaurės Amerika (NANP)
        "7",   # Rusija / Kazachstanas
        # 2 skaitmenys
        "20",  # Egiptas
        "27",  # Pietų Afrika
        "30",  # Graikija
        "31",  # Nyderlandai
        "32",  # Belgija
        "33",  # Prancūzija
        "34",  # Ispanija
        "36",  # Vengrija
        "39",  # Italija
        "40",  # Rumunija
        "41",  # Šveicarija
        "43",  # Austrija
        "44",  # JK
        "45",  # Danija
        "46",  # Švedija
        "47",  # Norvegija
        "48",  # Lenkija
        "49",  # Vokietija
        "51",  # Peru
        "52",  # Meksika
        "53",  # Kuba
        "54",  # Argentina
        "55",  # Brazilija
        "56",  # Čilė
        "57",  # Kolumbija
        "58",  # Venesuela
        "60",  # Malaizija
        "61",  # Australija
        "62",  # Indonezija
        "63",  # Filipinai
        "64",  # Naujoji Zelandija
        "65",  # Singapūras
        "66",  # Tailandas
        "81",  # Japonija
        "82",  # Pietų Korėja
        "84",  # Vietnamas
        "86",  # Kinija
        "90",  # Turkija
        "91",  # Indija
        "92",  # Pakistanas
        "93",  # Afganistanas
        "94",  # Šri Lanka
        "95",  # Mianmaras
        "98",  # Iranas
        # 3 skaitmenys
        "210",  # (nepriskirtas — paliktas dėl užpildymo išsamumo)
        "211",  # Pietų Sudanas
        "212",  # Marokas
        "213",  # Alžyras
        "216",  # Tunisas
        "218",  # Libija
        "220",  # Gambija
        "221",  # Senegalas
        "222",  # Mauritanija
        "223",  # Malis
        "224",  # Gvinėja
        "225",  # Dramblio Kaulo Krantas
        "226",  # Burkina Fasas
        "227",  # Nigeris
        "228",  # Togas
        "229",  # Beninas
        "230",  # Mauricijus
        "231",  # Liberija
        "232",  # Siera Leonė
        "233",  # Gana
        "234",  # Nigerija
        "235",  # Čadas
        "236",  # Centrinės Afrikos Respublika
        "237",  # Kamerūnas
        "238",  # Žaliasis Kyšulys
        "239",  # San Tomė ir Prinsipė
        "240",  # Pusiaujo Gvinėja
        "241",  # Gabonas
        "242",  # Kongo Respublika
        "243",  # Kongo DR
        "244",  # Angola
        "245",  # Bisau Gvinėja
        "246",  # Diego Garsija
        "247",  # Dangun Žengimo sala
        "248",  # Seišeliai
        "249",  # Sudanas
        "250",  # Ruanda
        "251",  # Etiopija
        "252",  # Somalis
        "253",  # Džibutis
        "254",  # Kenija
        "255",  # Tanzanija
        "256",  # Uganda
        "257",  # Burundis
        "258",  # Mozambikas
        "260",  # Zambija
        "261",  # Madagaskaras
        "262",  # Reunjonas / Majota
        "263",  # Zimbabvė
        "264",  # Namibija
        "265",  # Malavis
        "266",  # Lesotas
        "267",  # Botsvana
        "268",  # Esvantinis
        "269",  # Komorai
        "290",  # Šventosios Elenos sala
        "291",  # Eritrėja
        "297",  # Aruba
        "298",  # Farerų salos
        "299",  # Grenlandija
        "350",  # Gibraltaras
        "351",  # Portugalija
        "352",  # Liuksemburgas
        "353",  # Airija
        "354",  # Islandija
        "355",  # Albanija
        "356",  # Malta
        "357",  # Kipras
        "358",  # Suomija
        "359",  # Bulgarija
        "370",  # Lietuva
        "371",  # Latvija
        "372",  # Estija
        "373",  # Moldova
        "374",  # Armėnija
        "375",  # Baltarusija
        "376",  # Andora
        "377",  # Monakas
        "378",  # San Marinas
        "380",  # Ukraina
        "381",  # Serbija
        "382",  # Juodkalnija
        "383",  # Kosovas
        "385",  # Kroatija
        "386",  # Slovėnija
        "387",  # Bosnija ir Hercegovina
        "389",  # Šiaurės Makedonija
        "420",  # Čekija
        "421",  # Slovakija
        "423",  # Lichtenšteinas
        "500",  # Folklando salos
        "501",  # Belizas
        "502",  # Gvatemala
        "503",  # Salvadoras
        "504",  # Hondūras
        "505",  # Nikaragva
        "506",  # Kosta Rika
        "507",  # Panama
        "508",  # Sen Pjeras ir Mikelonas
        "509",  # Haitis
        "590",  # Gvadelupas
        "591",  # Bolivija
        "592",  # Gajana
        "593",  # Ekvadoras
        "594",  # Prancūzų Gviana
        "595",  # Paragvajus
        "596",  # Martinika
        "597",  # Surinamas
        "598",  # Urugvajus
        "599",  # Karibų Nyderlandai
        "670",  # Rytų Timoras
        "672",  # Norfolko sala
        "673",  # Brunėjus
        "674",  # Nauru
        "675",  # Papua Naujoji Gvinėja
        "676",  # Tonga
        "677",  # Saliamono salos
        "678",  # Vanuatu
        "679",  # Fidžis
        "680",  # Palau
        "681",  # Valis ir Futūna
        "682",  # Kuko salos
        "683",  # Niujė
        "685",  # Samoa
        "686",  # Kiribatis
        "687",  # Naujoji Kaledonija
        "688",  # Tuvalu
        "689",  # Prancūzų Polinezija
        "690",  # Tokelau
        "691",  # Mikronezija
        "692",  # Maršalo salos
        "850",  # Šiaurės Korėja
        "852",  # Honkongas
        "853",  # Makao
        "855",  # Kambodža
        "856",  # Laosas
        "880",  # Bangladešas
        "886",  # Taivanas
        "960",  # Maldyvai
        "961",  # Libanas
        "962",  # Jordanija
        "963",  # Sirija
        "964",  # Irakas
        "965",  # Kuveitas
        "966",  # Saudo Arabija
        "967",  # Jemenas
        "968",  # Omanas
        "970",  # Palestinos teritorijos
        "971",  # JAE
        "972",  # Izraelis
        "973",  # Bahreinas
        "974",  # Kataras
        "975",  # Butanas
        "976",  # Mongolija
        "977",  # Nepalas
        "992",  # Tadžikistanas
        "993",  # Turkmėnistanas
        "994",  # Azerbaidžanas
        "995",  # Gruzija
        "996",  # Kirgizija
        "998",  # Uzbekistanas
    ]
)


class PhoneFormatError(ValueError):
    """Iškyla, kai neapdorota telefono eilutė negali būti konvertuota į E.164 formatą."""


def _strip_formatting(raw: str) -> str:
    """Pašalina tarpus, brūkšnelius, taškus, skliaustus ir pasirenkamus pirmaujančius nulius po '00'."""
    # Normalizuoti įprastus skyriklius
    cleaned = re.sub(r"[\s\-\.\(\)\/]", "", raw)
    return cleaned


def _detect_country_code(digits: str) -> tuple[str, str]:
    """Grąžina (šalies_kodas, abonemento_numeris) iš skaitmenų eilutės, prasidedančios po '+'.

    Pirma bando 3 skaitmenų kodus, paskui 2 skaitmenų, tada 1 skaitmens (ilgiausias atitikmuo laimi).
    Iškelia PhoneFormatError, jei nerandamas galiojantis kodas.
    """
    for length in (3, 2, 1):
        prefix = digits[:length]
        if prefix in VALID_COUNTRY_CODES:
            return prefix, digits[length:]
    raise PhoneFormatError(
        f"No valid ITU country code found in '{digits}'. "
        "Supply `default_country_code` to handle local-format numbers."
    )


def format_e164(
    raw: str,
    *,
    default_country_code: str | None = None,
) -> str:
    """Formatuoja *raw* į E.164 (+CC + abonentas, be tarpų/brūkšnelių).

    Tvarko šias įprastas įvesties formas:
        "+370 612-34567"       -> "+37061234567"
        "0037061234567"        -> "+37061234567"  (TRS 00 prefiksas)
        "61234567"             -> "+37061234567"  (reikia default_country_code="370")
        "+1 (202) 555-0173"   -> "+12025550173"

    Pastaba apie magistralinius prefiksus: kai kurios šalys naudoja magistralinį
    skaitmenį (pvz., Lietuvos "8", Vokietijos "0"), kurį reikia pašalinti
    skambinant tarptautiniu mastu. Ši funkcija magistralinių prefiksų automatiškai
    NEŠALINA — šaukliai patys privalo pašalinti pirmaujantį magistralinį skaitmenį
    prieš perduodami vietinį numerį. Pavyzdys: lietuviškas "861234567" ->
    pašalinti pirmaujantį "8" -> "61234567"
    -> format_e164("61234567", default_country_code="370").

    Args:
        raw: Neapdorota telefono eilutė iš vartotojo įvesties ar duomenų bazės lauko.
        default_country_code: ITU šalies kodas (tik skaitmenys, be '+'), pridedamas
            kai *raw* neturi tarptautinio prefikso. Pavyzdys: "370" Lietuvai.

    Returns:
        E.164 eilutė, pvz. "+37061234567".

    Raises:
        PhoneFormatError: Jei numerio nepavyksta išanalizuoti arba šalies kodas
            neatpažintas / abonemento dalis yra neįtikėtinai trumpa ar ilga.
    """
    if not isinstance(raw, str):
        raise PhoneFormatError(f"Expected str, got {type(raw).__name__}.")

    stripped = _strip_formatting(raw.strip())

    if not stripped:
        raise PhoneFormatError("Phone number is empty after stripping formatting.")

    # Nustatyti, ar įvestyje jau yra tarptautinis prefiksas.
    if stripped.startswith("+"):
        # Pvz. "+37061234567" arba "+1 (202) 555-0173" (jau išvalytas aukščiau)
        digit_part = stripped[1:]
        if not digit_part.isdigit():
            raise PhoneFormatError(
                f"Non-digit characters remain after '+': '{digit_part}'."
            )
        country_code, subscriber = _detect_country_code(digit_part)
    elif stripped.startswith("00"):
        # TRS prefiksas (įprastas Europoje): 0037061234567
        digit_part = stripped[2:]
        if not digit_part.isdigit():
            raise PhoneFormatError(
                f"Non-digit characters remain after '00' IDD prefix: '{digit_part}'."
            )
        country_code, subscriber = _detect_country_code(digit_part)
    else:
        # Vietinis/nacionalinis formatas — reikia numatytojo šalies kodo
        if not stripped.isdigit():
            raise PhoneFormatError(
                f"Non-digit characters remain in local-format number: '{stripped}'."
            )
        if default_country_code is None:
            raise PhoneFormatError(
                "No international prefix found (not '+' or '00') and "
                "`default_country_code` was not supplied."
            )
        dcc = default_country_code.lstrip("+").strip()
        if not dcc.isdigit():
            raise PhoneFormatError(
                f"`default_country_code` must contain digits only, got '{dcc}'."
            )
        if dcc not in VALID_COUNTRY_CODES:
            raise PhoneFormatError(
                f"'{dcc}' is not a recognised ITU country code."
            )
        country_code = dcc
        subscriber = stripped

    # Patikrinti abonemento ilgį: E.164 iš viso ≤ 15 skaitmenų; CC jau ≥ 1.
    total_digits = len(country_code) + len(subscriber)
    if len(subscriber) < 4:
        raise PhoneFormatError(
            f"Subscriber number '{subscriber}' is too short (min 4 digits)."
        )
    if total_digits > 15:
        raise PhoneFormatError(
            f"Number exceeds E.164 maximum of 15 digits (got {total_digits})."
        )

    return f"+{country_code}{subscriber}"
