"""
Navigation helpers for the single-window Fidelity Trader+ UIA layout.

All panels (Quote, Level 2, Orders) live as siblings inside one ScrollViewer
within a single WinUIDesktopWin32WindowClass window. There are NO separate
top-level windows for each panel.

Observed hierarchy (0-based depth from app.top_window()):
  [Window] WinUIDesktopWin32WindowClass
    [Pane]  Microsoft.UI.Content.DesktopChildSiteBridge
      [Pane]  InputSiteWindowClass
        [Custom]                          ← panel_custom
          [Pane/ScrollViewer]             ← scroll_viewer  (flat panel siblings inside)
          [List/ListView]                 ← l2_list        (Level 2 depth rows)
"""
from __future__ import annotations


def _site_pane(app):
    main = app.top_window()
    try:
        return main.child_window(class_name="InputSiteWindowClass")
    except Exception:
        pass
    try:
        for d in main.descendants(control_type="Pane"):
            try:
                if d.element_info.class_name == "InputSiteWindowClass":
                    return d
            except Exception:
                continue
    except Exception:
        pass
    raise LookupError(
        "InputSiteWindowClass pane not found. "
        "Is Fidelity Trader+ a WinUI app on this machine?"
    )


def get_panel_container(app) -> tuple:
    """
    Return (panel_custom, scroll_viewer, l2_list):
    - panel_custom   : the Custom control that owns the ScrollViewer + ListView
    - scroll_viewer  : Pane/ScrollViewer whose flat children are all panel controls
    - l2_list        : List/ListView containing Level 2 rows (may be None)
    """
    site = _site_pane(app)
    try:
        customs = site.children(control_type="Custom")
        if not customs:
            raise LookupError("No Custom child found under InputSiteWindowClass")
        custom = customs[0]
    except LookupError:
        raise
    except Exception as exc:
        raise LookupError("Cannot enumerate InputSiteWindowClass children") from exc

    sv = None
    l2_list = None
    try:
        for child in custom.children():
            try:
                ctype = child.element_info.control_type
            except Exception:
                continue
            if ctype == "Pane" and sv is None:
                sv = child
            elif ctype == "List" and l2_list is None:
                l2_list = child
    except Exception as exc:
        raise LookupError("Cannot enumerate panel Custom children") from exc

    if sv is None:
        raise LookupError("ScrollViewer (Pane) not found inside panel Custom control")

    return custom, sv, l2_list


def sv_children(sv) -> list:
    """Return all direct children of the ScrollViewer, or [] on error."""
    try:
        return sv.children()
    except Exception:
        return []
