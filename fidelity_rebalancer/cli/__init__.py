from pathlib import Path

_PKG_ROOT = Path(__file__).parent.parent


def resolve_path(p: str) -> str:
    """Resolve a file path argument: try as-is first, then relative to fidelity_rebalancer/."""
    path = Path(p)
    if path.exists():
        return str(path)
    alt = _PKG_ROOT / p
    if alt.exists():
        return str(alt)
    return str(path)


def resolve_output_path(p: str) -> str:
    """Resolve an output path: if the parent dir doesn't exist, try under fidelity_rebalancer/."""
    path = Path(p)
    if path.parent.exists():
        return str(path)
    alt = _PKG_ROOT / p
    if alt.parent.exists():
        return str(alt)
    return str(path)
