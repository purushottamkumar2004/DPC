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


@register.simple_tag
def spark(values, width=64, height=16):
    """A sparkline of the last hour as plain inline SVG (D-098 5); nothing for < 2 samples."""
    from django.utils.safestring import mark_safe
    values = [float(v) for v in (values or []) if v is not None]
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    step = width / (len(values) - 1)
    points = " ".join(f"{i * step:.1f},{height - 1 - (v - low) / span * (height - 2):.1f}" for i, v in enumerate(values))
    return mark_safe(f'<svg class="spark" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
                     f'aria-hidden="true"><polyline points="{points}" fill="none" stroke="currentColor" '
                     f'stroke-width="1.2" stroke-linejoin="round" stroke-linecap="round"/></svg>')


@register.filter
def lvl(level):
    """Level -> colour class: only ok / warning / error / off carry a colour (D-098 2)."""
    return {"ok": "c-ok", "info": "c-ok", "warning": "c-warn", "error": "c-err", "off": "c-off"}.get(level, "")
