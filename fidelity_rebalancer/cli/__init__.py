from pathlib import Path

_PKG_ROOT = Path(__file__).parent.parent


def resolve_path(p: str) -> Path:
    """Resolve a file path argument: try as-is first, then relative to fidelity_rebalancer/."""
    path = Path(p)
    if path.exists():
        return path.resolve()
    alt = _PKG_ROOT / p
    if alt.exists():
        return alt.resolve()
    return path.resolve()


def resolve_output_path(p: str) -> Path:
    """Resolve an output path: if the parent dir doesn't exist, try under fidelity_rebalancer/."""
    path = Path(p)
    if path.parent.exists():
        return path.resolve()
    alt = _PKG_ROOT / p
    if alt.parent.exists():
        return alt.resolve()
    return path.resolve()
