"""Small UI capture bridge so the existing trading cycle can run behind FastAPI."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import date
import json
import math
import re
from types import SimpleNamespace

import pandas as pd


class SessionState(dict):
    __getattr__ = dict.get

    def __setattr__(self, key, value):
        self[key] = value


session_state = SessionState()
secrets = {}
_settings = {}
_actions = set()
_controls = []
_components = []


def begin(settings=None, actions=None):
    global _settings, _actions, _controls, _components
    _settings = settings or {}
    _actions = set(actions or [])
    _controls, _components = [], []


def result():
    return {"controls": _controls, "components": _components}


def _key(label, key):
    return str(key or re.sub(r"[^a-z0-9]+", "_", str(label).lower()).strip("_"))


def _control(kind, label, value, *, key=None, options=None, min_value=None,
             max_value=None, step=None, disabled=False, input_type=None):
    control_key = _key(label, key)
    current = _settings.get(control_key, value)
    if kind == "button":
        current = str(label) in _actions
    spec = {"kind": kind, "label": str(label), "key": control_key,
            "value": current, "disabled": bool(disabled)}
    if options is not None:
        spec["options"] = list(options)
    if min_value is not None:
        spec["min"] = min_value
    if max_value is not None:
        spec["max"] = max_value
    if step is not None:
        spec["step"] = step
    if input_type:
        spec["input_type"] = input_type
    _controls.append(spec)
    return _parse(kind, current, value)


def _parse(kind, value, default):
    if kind == "checkbox":
        return value if isinstance(value, bool) else str(value).lower() in {"true", "1", "on"}
    if kind in {"slider", "number_input"}:
        try:
            return type(default)(value)
        except (TypeError, ValueError):
            return default
    if kind == "date_input":
        try:
            return date.fromisoformat(str(value)) if value else default
        except (TypeError, ValueError):
            return default
    return value


def _component(kind, **data):
    _components.append({"type": kind, **_json_safe(data)})


def _json_safe(value):
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _heading(text): _component("heading", text=text)
def set_page_config(**kwargs): pass
def title(text): _heading(text)
def header(text): _heading(text)
def subheader(text): _heading(text)
def caption(text): _component("text", text=text, style="caption")
def markdown(text, **kwargs): _component("text", text=text, style="markdown")
def info(text): _component("notice", level="info", text=text)
def warning(text): _component("notice", level="warning", text=text)
def error(text): _component("notice", level="error", text=text)
def success(text): _component("notice", level="success", text=text)
def divider(): _component("divider")
def write(text): _component("text", text=str(text), style="body")


def metric(label, value, delta=None, **kwargs):
    _component("metric", label=label, value=value, delta=delta)


def dataframe(data, **kwargs):
    if isinstance(data, pd.DataFrame):
        columns = [str(col) for col in data.columns]
        rows = data.astype(object).where(pd.notna(data), None).to_dict("records")
    elif isinstance(data, dict):
        columns, rows = list(data), [data]
    else:
        rows = list(data or [])
        columns = list(rows[0]) if rows and isinstance(rows[0], dict) else []
    _component("table", columns=columns, rows=rows, hide_index=kwargs.get("hide_index", False))


def plotly_chart(figure, **kwargs):
    try:
        spec = json.loads(figure.to_json())
    except Exception:
        spec = {"data": [], "layout": {}}
    _component("chart", figure=spec)


def columns(spec, **kwargs):
    count = spec if isinstance(spec, int) else len(spec)
    return [SimpleNamespace(metric=metric, dataframe=dataframe, markdown=markdown,
                            caption=caption, write=write, plotly_chart=plotly_chart)
            for _ in range(count)]


def button(label, **kwargs):
    return _control("button", label, False, disabled=kwargs.get("disabled", False))


def checkbox(label, value=False, **kwargs):
    return _control("checkbox", label, value, key=kwargs.get("key"))


def selectbox(label, options, index=0, **kwargs):
    options = list(options)
    value = options[index] if options and 0 <= index < len(options) else ""
    return _control("selectbox", label, value, key=kwargs.get("key"), options=options)


def slider(label, min_value=None, max_value=None, value=None, step=None, **kwargs):
    return _control("slider", label, value, key=kwargs.get("key"), min_value=min_value,
                    max_value=max_value, step=step)


def number_input(label, min_value=None, max_value=None, value=0, step=None, **kwargs):
    return _control("number_input", label, value, key=kwargs.get("key"), min_value=min_value,
                    max_value=max_value, step=step)


def text_input(label, value="", **kwargs):
    return _control("text_input", label, value, key=kwargs.get("key"),
                    input_type=kwargs.get("type", "text"))


def date_input(label, value=None, **kwargs):
    return _control("date_input", label, value or date.today(), key=kwargs.get("key"))


def expander(label, **kwargs):
    _component("expander", label=label)
    return nullcontext()


def fragment(**kwargs):
    return lambda function: function


sidebar = nullcontext()
