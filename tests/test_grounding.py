from datetime import date

from curator.grounding import verify

LOC = {"p1": "Azione 1 04/02/2026 SOSTITUITA LA RESISTENZA DI FINE LINEA SULL'ASSE AX4. Ore 6,5 – Rif. EXT 5001",
       "p2": "The drive must be configured with the DriveSetup tool before use."}
D = date(2026, 2, 4)


def fact(**kw):
    base = {"concept_key": {"number": "7001"}, "location": "p1", "quote_lang": "it", "date": None,
            "time_expression_raw": None,
            "quote": "04/02/2026 SOSTITUITA LA RESISTENZA DI FINE LINEA SULL'ASSE AX4",
            "text": "Il 04/02/2026 è stata sostituita la resistenza di fine linea sull'asse AX4."}
    base.update(kw)
    return base


def test_valid_fact():
    v = verify(fact(date="2026-02-04"), LOC, D, "TICKET 7001")
    assert v.ok and v.date == "2026-02-04" and not v.date_uncertain


def test_invented_quote_rejected():
    assert not verify(fact(quote="il firmware è 1A20"), LOC, D, "x").ok


def test_wrong_location_rejected():
    v = verify(fact(location="p9"), LOC, D, "x")
    assert not v.ok and "location" in v.reason
    v = verify(fact(location="p2"), LOC, D, "x")
    assert not v.ok and "not found" in v.reason


def test_number_not_in_quote_rejected():
    v = verify(fact(text="Sostituita la resistenza sull'asse AX4, 8 ore di lavoro."), LOC, D, "x")
    assert not v.ok and "numbers" in v.reason


def test_code_not_in_quote_rejected():
    v = verify(fact(text="Sostituita la resistenza sull'asse AX5."), LOC, D, "x")
    assert not v.ok and "codes" in v.reason


def test_date_not_in_quote_rejected():
    v = verify(fact(text="Il 05/02/2026 è stata sostituita la resistenza sull'asse AX4."), LOC, D, "x")
    assert not v.ok and "date" in v.reason


def test_name_not_in_quote_rejected():
    v = verify(fact(text="Rossi ha sostituito la resistenza sull'asse AX4, come dice Bianchi."), LOC, D, "x")
    assert not v.ok and "names" in v.reason


def test_normalization_decimal_and_typography():
    f = fact(quote="Ore 6,5 – Rif. EXT 5001", text="Lavoro di 6.5 ore (EXT5001).")
    assert verify(f, LOC, D, "x").ok


def test_language_mismatch_rejected():
    f = fact(location="p2", quote="The drive must be configured with the DriveSetup tool before use.", quote_lang="it",
             text="Il drive va configurato con il tool DriveSetup prima dell'uso.")
    v = verify(f, LOC, D, "x")
    assert not v.ok and "language" in v.reason
    assert verify({**f, "quote_lang": "en"}, LOC, D, "x").ok


def test_invented_date_field_becomes_uncertain():
    v = verify(fact(date="2026-03-01"), LOC, D, "x")
    assert v.ok and v.date is None and v.date_uncertain and v.notes


def test_key_not_in_source_is_only_a_note():
    v = verify(fact(concept_key={"number": "7777"}), LOC, D, "x", doc_text=LOC["p1"])
    assert v.ok and any("7777" in n for n in v.notes)


def test_key_declared_as_alias_is_not_noted():
    v = verify(fact(concept_key={"number": "BL2"}), LOC, D, "x", doc_text=LOC["p1"], known={"BL2"})
    assert v.ok and v.notes == []


def test_event_date_found_elsewhere_in_chunk():
    locs = {"p2": "24/10/2025 - Tecnico 1\nAbbiamo montato l'asse AX4.",
            "p3": "Abbiamo ripristinato il collegamento originale con AX4."}
    f = fact(location="p3", quote="Abbiamo ripristinato il collegamento originale con AX4.",
             text="Ripristinato il collegamento originale con AX4.", date="2025-10-24")
    v = verify(f, locs, date(2025, 10, 27), "x")
    assert v.ok and v.date == "2025-10-24" and not v.date_uncertain


def test_relative_dates_resolved_by_code():
    loc = {"s-1": "Il tecnico torna domani per sostituire il cavo dell'asse AX4."}
    f = fact(location="s-1", quote="Il tecnico torna domani per sostituire il cavo dell'asse AX4.",
             text="Il tecnico torna per sostituire il cavo dell'asse AX4.", date="2026-10-16",
             time_expression_raw="domani")
    v = verify(f, loc, date(2026, 10, 14), "Mail del 14/10")
    assert v.ok and v.date == "2026-10-15" and not v.date_uncertain      # "domani" on 14/10 = 15/10
    assert any("replaced" in n for n in v.notes)                          # the LLM date does not count
    v = verify({**f, "time_expression_raw": "prima o poi", "date": None}, loc, date(2026, 10, 14), "x")
    assert v.ok and v.date is None and v.date_uncertain


def test_relative_expression_refers_to_the_date_written_in_the_block():
    # report written on 30/10, paragraph dated 27/10: "solo oggi" is 27/10, not the date of the report
    loc = {"p3": "27/10/2025 Reset", "p4": "Solo oggi, alle 15:42, il reset 24V è stato eseguito 99 volte."}
    f = fact(location="p4", quote="il reset 24V è stato eseguito 99 volte", text="Il reset 24V è stato eseguito 99 volte.",
             date="2025-10-27", time_expression_raw="Solo oggi")
    v = verify(f, loc, date(2025, 10, 30), "Report")
    assert v.ok and v.date == "2025-10-27" and not v.date_uncertain
    # "domani" in the same paragraph: the code does the arithmetic from the written date
    v = verify({**f, "time_expression_raw": "domani"}, loc, date(2025, 10, 30), "Report")
    assert v.date == "2025-10-28" and any("written in the block" in n for n in v.notes)
    # a date that is not written in the block does not count: back to the source date
    v = verify({**f, "date": "2025-10-29", "time_expression_raw": "oggi"}, loc, date(2025, 10, 30), "Report")
    assert v.date == "2025-10-30"


def test_resolve_relative_weekdays():
    from curator.temporal import resolve_relative
    assert resolve_relative("lunedì", date(2025, 8, 29)) == date(2025, 9, 1)        # Friday → next Monday
    assert resolve_relative("lunedì scorso", date(2025, 8, 29)) == date(2025, 8, 25)
    assert resolve_relative("last monday", date(2025, 8, 29)) == date(2025, 8, 25)
    assert resolve_relative("fine mese", date(2026, 2, 10)) == date(2026, 2, 28)
    assert resolve_relative("ieri pomeriggio", date(2026, 3, 1)) == date(2026, 2, 28)
    assert resolve_relative("tomorrow morning", date(2026, 3, 1)) == date(2026, 3, 2)


def test_values_in_the_same_sentence_of_a_cut_quote_are_accepted():
    # a PDF line break cuts the quote before the axis code, which is in the same sentence
    loc = {"p1": "Rimane il problema con l'asse\nAX9 che scompare dalla rete. L'asse AX12 invece funziona."}
    f = fact(location="p1", quote="Rimane il problema con l'asse", text="L'asse AX9 scompare dalla rete.")
    assert verify(f, loc, D, "x").ok
    # a value of the next sentence still does not count
    v = verify({**f, "text": "L'asse AX12 scompare dalla rete."}, loc, D, "x")
    assert not v.ok and "codes" in v.reason
    # a table row is one location without full stops: the name in another cell counts
    row = {"r-dati-5": "SW1 | 0 | Status word | bit 3 | Ready | read only"}
    f = fact(location="r-dati-5", quote="bit 3 | Ready", text="Il bit 3 della Status word è Ready.")
    assert verify(f, row, D, "x").ok
    # a number added by the LLM (step numbering) is not in the source
    loc = {"p1": "ControlWord = 0x06 e attendere StatusWord con bit0 e bit5."}
    f = fact(location="p1", quote="ControlWord = 0x06 e attendere StatusWord con bit0 e bit5",
             text="Passo 1: ControlWord = 0x06 e attendere StatusWord con bit0 e bit5.")
    assert not verify(f, loc, D, "x").ok
