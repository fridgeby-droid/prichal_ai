from datetime import date, timedelta


def chunks(date_from: date, date_to: date, chunk_days: int = 7):
    current = date_from
    result = []

    while current <= date_to:
        chunk_to = min(
            current + timedelta(days=chunk_days - 1),
            date_to,
        )

        result.append(
            (
                current,
                chunk_to,
                chunk_to + timedelta(days=1),
            )
        )

        current = chunk_to + timedelta(days=1)

    return result


def test_july_first_block_fetches_tail_day():
    result = chunks(
        date(2026, 7, 1),
        date(2026, 7, 14),
    )

    assert result[0] == (
        date(2026, 7, 1),
        date(2026, 7, 7),
        date(2026, 7, 8),
    )

    assert result[1] == (
        date(2026, 7, 8),
        date(2026, 7, 14),
        date(2026, 7, 15),
    )


def test_july_to_sep22_is_84_days_12_chunks():
    result = chunks(
        date(2026, 7, 1),
        date(2026, 9, 22),
    )

    assert (
        date(2026, 9, 22)
        - date(2026, 7, 1)
    ).days + 1 == 84

    assert len(result) == 12
