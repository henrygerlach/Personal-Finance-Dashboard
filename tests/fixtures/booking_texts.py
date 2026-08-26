"""
Real Comdirect booking lines, with the expected merchant key for each.

Taken verbatim from the statement data and then anonymised: the names of
private individuals are replaced with invented ones and IBANs with
non-routable placeholders. Everything that the normalizer actually reads —
field labels, reference numbers, card numbers, the line wrapping, the encoding
damage — is untouched, because that is what the tests are about.

Each entry is ``(booking_text, process, expected_key, what_it_covers)``.
"""

CASES = [
    # --- Direct debits: the merchant appears twice, once clean --------------
    (
        "Auftraggeber: EDEKA M. PETERSEN Buchungstext: EDEKA M. PETERSEN "
        "HAMBURG KARTE 0132 28122022 KDN-REF 000000570661 "
        "Ref. JP22236453035320/128641",
        "Lastschrift / Belastung",
        "EDEKA M. PETERSEN",
        "ordering party wins over the repeated, noisier booking text",
    ),
    (
        "Auftraggeber: ALDI GMBH & CO. KG SEE Buchungstext: ALDI GMBH & CO. KG "
        "SEE HAMBURG KARTE 0132 19122022 KDN-REF 000000955461 "
        "Ref. JF22235553109323/147342",
        "Lastschrift / Belastung",
        "ALDI GMBH & CO. KG SEE",
        "ampersands and legal forms are part of the name",
    ),
    (
        "Auftraggeber: ACADEMY FAHRSCHULE HOHELUFTGBR MERSCH UND KOLLEGEN "
        "Buchungstext: Rechnung 18760 vom 17.05.2024",
        "Lastschrift / Belastung",
        "ACADEMY FAHRSCHULE HOHELUFTGBR MERSCH UND KOLLEGEN",
        "invoice number and date are dropped",
    ),
    (
        "Auftraggeber: 1,75 PROZ.AUSLANDSENTGELT Buchungstext: "
        "1,75 PROZ.AUSLANDSENTGELT  KARTE 0132 Ref. HY22234053119413/7423",
        "Lastschrift / Belastung",
        "1,75 PROZ.AUSLANDSENTGELT",
        "a fee line is a stable key even though it is not a merchant",
    ),
    # --- Card payments -----------------------------------------------------
    (
        " SVAADISH HOHELUFT GMBH HAMBURG 000 ",
        "Visa-Umsatz",
        "SVAADISH HOHELUFT GMBH HAMBURG",
        "the trailing zero group Visa exports append",
    ),
    (
        " MEDIA MARKT ",
        "Kartenumsatz",
        "MEDIA MARKT",
        "a bare card payment passes through untouched",
    ),
    (
        " DM DROGERIE SAGT DANKE ",
        "Kartenumsatz",
        "DM DROGERIE",
        "a marketing suffix is not part of the merchant",
    ),
    (
        " SumUp Hansa Kebab ",
        "Kartenumsatz",
        "HANSA KEBAB",
        "the card terminal provider is stripped, the shop behind it kept",
    ),
    (
        " SumUp Backerei Allaf ",
        "Kartenumsatz",
        "BACKEREI ALLAF",
        "the same, for a second SumUp merchant",
    ),
    (
        " HAMBURGER SPARKASSE AG 280 901124 000 ",
        "Barauszahlung",
        "HAMBURGER SPARKASSE AG 280",
        "an ATM keeps its short branch number but loses the terminal number",
    ),
    (
        " Saigon Branch Saigon Branc690Betrag: -1500000,00 VNDKurs: 25339,0000 ",
        "Barauszahlung",
        "SAIGON BRANCH SAIGON BRANC690",
        "a foreign-currency withdrawal appends amount and rate",
    ),
    # --- PayPal: the merchant hides behind the processor --------------------
    (
        "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
        "1023434825177 PP.5189.PP . Spotify AB, Ihr Einkauf bei "
        "Ref. XY22234453035320/1",
        "Lastschrift / Belastung",
        "SPOTIFY AB",
        "PayPal is the processor, Spotify is the merchant",
    ),
    (
        "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
        "1022650307427 . Adidas AG, Ihr Ein kauf bei Adidas AG "
        "Ref. IK22227671938321/5225",
        "Lastschrift / Belastung",
        "ADIDAS AG",
        "the wrap falls inside 'Einkauf' after the third letter",
    ),
    (
        "Auftraggeber: PayPal (Europe) S.a.r.l. et Cie., S.C.A. Buchungstext: "
        "PP.5189.PP . mymuesli AG, Ihr Einka uf bei mymuesli AG "
        "Ref. IX22215472536670/9",
        "Lastschrift / Belastung",
        "MYMUESLI AG",
        "the wrap falls after the fifth letter",
    ),
    (
        "Auftraggeber: PayPal Europe S.a.r.l. et Cie S.C.A Buchungstext: "
        "1040676278748/. Uber, Ihr Einkauf b ei Uber "
        "Ref. AB2C218H1T851XQ9/98391",
        "Lastschrift / Belastung",
        "UBER",
        "the wrap falls inside 'bei', not inside 'Einkauf'",
    ),
    (
        "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
        ". Salonmeister GmbH, Ihr Einkauf be i Salonmeister GmbH "
        "Ref. IF22213072024214/2",
        "Lastschrift / Belastung",
        "SALONMEISTER GMBH",
        "the wrap falls before the last letter of 'bei'",
    ),
    (
        "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
        ". ASOS.com, Ihr Einkauf bei ASOS.co m Ref. JK22209571559119/3661",
        "Lastschrift / Belastung",
        "ASOS.COM",
        "a domain merchant keeps its dots instead of collapsing to its TLD",
    ),
    (
        "Auftraggeber: PayPal Europe S.a.r.l. et Cie S.C.A Buchungstext: "
        "1039831565228/PP.5189.PP/. www.stea mpowered.com, Ihr Ei nkauf bei "
        "Ref. 8A2C213J2F5CEXKC/7",
        "Lastschrift / Belastung",
        "WWW.STEA MPOWERED.COM",
        "a wrap inside a domain; the key strips spaces so it still collides",
    ),
    (
        "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
        "1024177396132 PP.5189.PP . , Ihr Ei nkauf bei "
        "Ref. J822235371402476/7550",
        "Lastschrift / Belastung",
        "",
        "Comdirect truncated the merchant away; there is nothing to classify",
    ),
    (
        "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
        "1021808533116 PP.5189.PP Ref. I92223071321924/1",
        "Lastschrift / Belastung",
        "",
        "the same, in the variant that has no merchant field at all",
    ),
    # --- Transfers ---------------------------------------------------------
    (
        # Anonymised: private individual, placeholder IBAN.
        "EmpfÜnger: Mara BeckerKto/IBAN: DE00000000000000000000 "
        "BLZ/BIC: DEUTDEDBHAM  Buchungstext: Rundfunk Ref. GR222046K0432479/2",
        "Übertrag / Überweisung",
        "MARA BECKER",
        "the next field label follows with no separating space",
    ),
    (
        # Anonymised: private individual.
        "Auftraggeber: Rieke Falkenberg Buchungstext: Weihnachtsgeld "
        "Ref. IQ222361D3733746/1",
        "Übertrag / Überweisung",
        "RIEKE FALKENBERG",
        "a private sender; the purpose text is not part of the key",
    ),
    (
        "Auftraggeber: DIFICO-DEUTSCHES INSTITUT F ÜR FINANZANALYSE UND "
        "COACHIN Buchungstext: Inflationsausgleich Ref. IG222357G1213590/1",
        "Übertrag / Überweisung",
        "DIFICO-DEUTSCHES INSTITUT F UER FINANZANALYSE UND COACHIN",
        "a mis-decoded umlaut is folded to its ASCII spelling",
    ),
    (
        "Auftraggeber: DIFICO-DEUTSCHES INSTITUT F UER FINANZANALYSE UND "
        "COACHIN Buchungstext: Gehalt Ref. IG222357G1213591/1",
        "Übertrag / Überweisung",
        "DIFICO-DEUTSCHES INSTITUT F UER FINANZANALYSE UND COACHIN",
        "the same company spelled the other way must produce the same key",
    ),
    # --- Bank's own charges and securities ----------------------------------
    (
        " Buchungstext: Entgelt Visa-Kreditkarte Zeitraum: 01.12.2023 bis "
        "31.12.2023 Ref. 6V2C1MIL31FF0QZO/242348",
        "Kontoführungsentgelt",
        "ENTGELT VISA-KREDITKARTE",
        "the billing period varies monthly and must not enter the key",
    ),
    (
        " Buchungstext: Kosten genutzte mobileTAN Vormonat 1 kostenpflichtige "
        "mobileTAN Ref. 9Z2C1MHT2V3EK0NH/328932",
        "Entgelte",
        "KOSTEN GENUTZTE MOBILETAN VORMONAT 1 KOSTENPFLICHTIGE MOBILETAN",
        "a fee description with no merchant at all is still a stable key",
    ),
    (
        " Buchungstext: X(IE)-MSCI WORLD 1C WPKNR: A1XB5U ISIN: IE00BJ0KDQ92 "
        "Ref. 2J5C21XS0BIG3MUL/2",
        "Wertpapiere",
        "X(IE)-MSCI WORLD 1C",
        "security identifiers are dropped, the fund name is the key",
    ),
    (
        " Buchungstext: LANG+SCHWARZ O.E. INDEX WPKNR: LS9TNE "
        "ISIN: DE000LS9TNE0 Ref. 5D5C1EKH517UGJMJ/2",
        "Wertpapiere",
        "LANG+SCHWARZ O.E. INDEX",
        "the same, for a second security",
    ),
    # --- Degenerate input ---------------------------------------------------
    ("", None, "", "an empty booking text"),
    ("   ", None, "", "whitespace only"),
    ("Ref. ABC123/4", None, "", "a reference and nothing else"),
]

#: Pairs that must collapse onto one cache key even though they are spelled
#: differently. Left is what one payment channel writes, right the other.
SAME_MERCHANT_PAIRS = [
    (
        "Auftraggeber: EDEKA BOECKER Buchungstext: EDEKA BOECKER HAMBURG "
        "KARTE 0132 23122022 KDN-REF 000000260698 Ref. HS22236253111512/1",
        " EDEKA BOECKER HAMBURG 000 ",
        "a direct debit omits the town, a card payment appends it",
    ),
    (
        "Auftraggeber: PayPal Europe Buchungstext: PP.5189.PP . "
        "www.stea mpowered.com, Ihr Einkauf bei Ref. AB1/2",
        "Auftraggeber: PayPal Europe Buchungstext: PP.5189.PP . "
        "www.steampowered.com, Ihr Einkauf bei Ref. AB1/3",
        "the line wrap fell inside the domain on one statement",
    ),
    (
        "Auftraggeber: LOGPAY F INANCIAL SERVICES GMBH Buchungstext: x",
        "Auftraggeber: LOGPAY FINANCIAL SE RVICES GMBH Buchungstext: x",
        "the line wrap fell at a different column on each statement",
    ),
]
