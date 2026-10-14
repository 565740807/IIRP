"""Phone push texts in the language chosen for pushes, from the same translation files as the
interface. A push carries public facts only: who, which stock, buy or sell, amount, trade and
publication dates, and the public link of the original filing or report (never a local address)."""

from decimal import Decimal, InvalidOperation

from iirp.messages import saved_name

DIGEST_LINES = 5


def _text(code, language, **params):
    return saved_name(f"notifications.push.{code}", language=language, **params)


def money(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return f"${number:,.0f}"


def _range(low, high, language):
    if low is None:
        return None
    if high is None:
        return _text("over", language, amount=money(low))
    return f"{money(low)}–{money(high)}"


def _dates(data, language):
    traded = data.get("traded_from")
    if traded and data.get("traded_to") and data["traded_to"] != traded:
        traded = f"{traded} – {data['traded_to']}"
    return _text("dates", language, traded=traded or "—", published=data.get("published") or "—")


def _side(side, language):
    return _text(f"side.{side}", language)


def title(notification, language):
    data, kind = notification.data, notification.kind
    if kind == "rule_executive":
        roles = "/".join(role.upper() for role in data["person"]["roles"]) or "CEO"
        return _text("executive_title", language, role=roles, ticker=data.get("ticker") or data.get("asset_name"),
                     amount=money(data["amount"]))
    if kind == "rule_cluster":
        return _text("cluster_title", language, count=data["people"], ticker=data.get("ticker") or data.get("asset_name"),
                     days=data["days"])
    person = data["person"]["name"]
    if data["source"] == "sec":
        sides = [side for side in ("buy", "sell") if data.get(side)]
        what = " / ".join(_side(side, language) for side in sides)
        heading = _text("trade_title", language, person=person, side=what, asset=data.get("ticker") or data.get("asset_name"))
    elif data["rows"] == 1:
        item = data["items"][0]
        heading = _text("trade_title", language, person=person, side=_side(item["side"], language),
                        asset=item.get("ticker") or item.get("asset_name"))
    else:
        parts = [_text(f"report_{side}_{'one' if data[side] == 1 else 'other'}", language, count=data[side])
                 for side in ("buys", "sells") if data.get(side)]
        heading = _text("report_title", language, person=person, rows=_text("join", language).join(parts))
    return _text("amended", language, title=heading) if data.get("amendment") else heading


def body(notification, language):
    data, kind = notification.data, notification.kind
    lines = []
    if kind == "rule_executive":
        lines.append(_text("executive_body", language, person=data["person"]["name"]))
    elif kind == "rule_cluster":
        lines.append(_text("cluster_body", language, names=", ".join(data["names"]), rows=data["rows"]))
        if data.get("amount"):
            lines.append(_text("amount", language, amount=money(data["amount"])))
    elif data["source"] == "sec":
        for side in ("buy", "sell"):
            part = data.get(side)
            if part:
                amount = money(part["amount"]) if part.get("amount") else "—"
                line = _text("sec_line", language, side=_side(side, language), amount=amount, rows=part["rows"])
                if side == "sell":
                    line += " · " + _text(f"plan.{part['plan']}", language)
                lines.append(line)
    else:
        for item in data["items"]:
            lines.append(" ".join(filter(None, [
                _side(item["side"], language), item.get("ticker") or item.get("asset_name"),
                _range(item.get("amount_low"), item.get("amount_high"), language)])))
        if data["rows"] > len(data["items"]):
            lines.append(_text("more_rows", language, count=data["rows"] - len(data["items"])))
    lines.append(_dates(data, language))
    return "\n".join(lines)


def payload(notification, topic, language):
    """One ntfy message (JSON published to the server root) for one notification."""
    message = {"topic": topic, "title": title(notification, language), "message": body(notification, language)}
    if notification.data.get("url"):
        message["click"] = notification.data["url"]
    return message


def digest(notifications, topic, language):
    """One ntfy message for several notifications of the same minute."""
    lines = [title(item, language) for item in notifications[:DIGEST_LINES]]
    if len(notifications) > DIGEST_LINES:
        lines.append(_text("more", language, count=len(notifications) - DIGEST_LINES))
    return {"topic": topic, "title": _text("digest_title", language, count=len(notifications)),
            "message": "\n".join(lines)}


def test(topic, language):
    return {"topic": topic, "title": _text("test_title", language), "message": _text("test_body", language)}
