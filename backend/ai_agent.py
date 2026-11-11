from collections.abc import Mapping, Sequence
from decimal import Decimal
import re


_WORDS = re.compile(r"[a-z0-9]+")


def answer_admin_query(
    question: str,
    requests: Sequence[Mapping[str, object]],
    available_pool_usd: Decimal,
) -> dict[str, object]:
    normalized_question = question.casefold()
    filtered = list(requests)

    locations = {
        str(request.get("location", "")).strip()
        for request in filtered
        if request.get("location")
    }
    question_terms = set(_WORDS.findall(normalized_question))
    location_matches = [
        (len(question_terms & set(_WORDS.findall(location.casefold()))), location)
        for location in locations
    ]
    location_matches = [match for match in location_matches if match[0] > 0]
    selected_location = (
        max(location_matches, key=lambda match: (match[0], -len(match[1])))[1]
        if location_matches
        else None
    )
    if selected_location:
        selected_terms = set(_WORDS.findall(selected_location.casefold()))
        filtered = [
            request
            for request in filtered
            if selected_terms
            & set(_WORDS.findall(str(request.get("location", "")).casefold()))
        ]

    if "paid" in normalized_question or "payout" in normalized_question:
        filtered = [
            request for request in filtered if request.get("status") == "Paid"
        ]
        label = "paid requests"
    elif "pending" in normalized_question or "verification" in normalized_question:
        filtered = [
            request
            for request in filtered
            if request.get("status") == "Pending Verification"
        ]
        label = "requests awaiting verification"
    else:
        label = "requests"

    if any(word in normalized_question for word in ("balance", "pool", "funds")):
        answer = f"Available platform pool balance: USD {available_pool_usd:.2f}."
    else:
        answer = f"There are {len(filtered)} {label}"
        if selected_location:
            answer += f" in {selected_location}"
        answer += "."
        if any(word in normalized_question for word in ("cost", "amount", "spend")):
            total = sum(
                (
                    Decimal(str(request.get("total_cost_usd") or "0"))
                    for request in filtered
                ),
                Decimal("0.00"),
            )
            answer += f" Their combined formula cost is USD {total:.2f}."

    return {
        "answer": answer,
        "request_count": len(filtered),
        "location": selected_location,
        "request_ids": [request.get("id") for request in filtered],
    }