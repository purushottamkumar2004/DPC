"""Small display helpers for the templates."""
import datetime
import re

from django import template

from monitor.overview import age_text

register = template.Library()


@register.filter
def age(seconds):
    """12 -> '12 s', 125 -> '2 min'."""
    return age_text(seconds)


@register.filter
def clock(epoch):
    """Epoch seconds -> '14:05:09' today, else '06 Nov 14:05'."""
    if not epoch:
        return "—"
    moment = datetime.datetime.fromtimestamp(float(epoch))
    if moment.date() == datetime.date.today():
        return moment.strftime("%H:%M:%S")
    return moment.strftime("%d %b %H:%M")


@register.filter
def num(value, digits=0):
    """A number with fixed digits, or '—'."""
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.{int(digits)}f}"
    except (TypeError, ValueError):
        return str(value)


@register.filter
def get(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def side_word(defect_side):
    return {"Left": "LEFT", "Right": "RIGHT"}.get(defect_side, defect_side)


@register.filter
def type_word(defect_type):
    """'Wheel_Skidding' -> 'Wheel skidding', 'WheelMissing' -> 'Wheel missing'."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(defect_type or "")).replace("_", " ")
    return text[:1].upper() + text[1:].lower() if text else "—"
