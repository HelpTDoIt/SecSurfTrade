# 12 — OCR Engine Migration: RapidOCR → Surya

**Status:** 2026-06-06. Design and implementation plan for replacing
`rapidocr-onnxruntime` (PaddleOCR-via-ONNX) with Surya OCR in
`adapters/atp_ocr.py`. Roadmap item **B-16**.

**Hard rule:** The app NEVER places, modifies, or cancels orders. OCR is
read-only input to the monitoring system. No data is exfiltrated; ATP
screenshots stay on-device.

---

## 0. Motivation and scope

### Why swap engines

RapidOCR was chosen early for its zero-native-dep profile (pure ONNX). Surya
is designed specifically for **structured document and grid layouts** — exactly
what ATP's Orders panel (dense tabular rows, fixed columns) and Level II panel
(two-sided depth grid) present.

Failure modes observed with RapidOCR on ATP:

| Failure | Example | Impact |
|---|---|---|
| Character substitution in dense numeric columns | `0` ↔ `O`, `1` ↔ `l`, `6` ↔ `b` | Misread share count or limit price → wrong order entry by human |
| Adjacent-cell merge in tight L2 bid/ask columns | `62.39 200` read as one token | L2 side parsed incorrectly |
| Blank detections on mid-capture rerenders | OCR returns empty for a valid row | Order missed; stall detection misses fill |

Surya addresses all three via a recognition head trained specifically on
tabular layouts.

### What changes — and what doesn't

Only **two functions** in one file are replaced. Everything else is unchanged.

| | |
|---|---|
| **Changed** | `_ocr_engine()` (singleton factory) + `_ocr_to_cells()` (result adapter) |
| **Unchanged** | `_Cell` NamedTuple · `_cluster_rows` · `_find_col_x` · `_crop_section` · `_parse_orders_from_rows` · `get_orders` · `get_level2` · `get_watchlist_positions` · all of `atp_watchlist.py`, `atp_level2.py`, `yfinance_fallback.py`, `monitor.py` |

The public adapter API (`get_orders`, `get_level2`, `get_watchlist_positions`)
is byte-compatible with the current version.

### Relationship to other OCR items

- **C-12 — Asynchronous OCR Workers:** wraps `_run_ocr()` in a thread/process
  pool. **Do B-16 first** — validate the new engine in the synchronous path
  before adding async complexity. C-12's wrapper code is engine-agnostic; no
  rework needed.
- **C-13 — Localized Region Caching:** caches `(panel_x, panel_y)` offsets
  after the first successful detection. Engine-agnostic; can land before or
  after B-16.

---

## 1. Current code seams

All in `fidelity_rebalancer/adapters/atp_ocr.py`:

```
_ocr_engine()  [lines 77–81]
    @lru_cache(maxsize=1) singleton.
    Returns: RapidOCR(det_model_path=None, det_limit_side_len=2400, det_limit_type="max")
    One call site: _run_ocr() (line 170).

_run_ocr(img, label)  [lines 165–176]
    Calls: result, _ = _ocr_engine()(img)
    Then:  cells = _ocr_to_cells(result or [])
    Returns: list[_Cell]
    → This is the stable interface; every caller receives list[_Cell].

_ocr_to_cells(result)  [lines 221–239]
    Input:  RapidOCR format — list of (bbox, text, score)
            bbox = [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]  (4-point quad, clockwise)
    Output: list[_Cell(x=xc, y=yc, text=t)]
            xc = pts[:,0].mean(), yc = pts[:,1].mean()

_Cell(NamedTuple)  [lines 215–218]
    x: float   # horizontal center (pixels from left edge of captured region)
    y: float   # vertical center   (pixels from top edge of captured region)
    text: str
```

**The migration contract:** `_ocr_to_cells` is replaced by `_surya_to_cells`.
It must produce `_Cell(x, y, text)` instances using **the same center-point
convention** — `x` = horizontal center, `y` = vertical center, in pixels
relative to the captured image region. The `y`-tolerance in `_cluster_rows`
(`y_tol=8`) and the column-boundary logic in `_find_col_x` depend on this.

---

## 2. Design decisions

### 2a. Surya API surface

Surya (https://github.com/VikParuchuri/surya) exposes:

```python
from surya.ocr import run_ocr
from surya.model.detection.model import load_model as load_det_model
from surya.model.detection.model import load_processor as load_det_processor
from surya.model.recognition.model import load_model as load_rec_model
from surya.model.recognition.processor import load_processor as load_rec_processor

results = run_ocr(
    [pil_image],          # list of PIL Images (one per image)
    [["en"]],             # list of language lists (one per image)
    det_model, det_processor,
    rec_model, rec_processor,
)
# results[0].text_lines: list[TextLine]
# TextLine.bbox:       [x1, y1, x2, y2]  (axis-aligned bounding box)
# TextLine.text:       str
# TextLine.confidence: float
```

Surya expects PIL Images. The existing pipeline provides numpy arrays (RGB,
from `PrintWindow`). Add a one-line conversion in `_run_ocr`:

```python
pil_img = Image.fromarray(img)   # img is already RGB uint8
```

`Image` is already imported (line 35 of current file).

### 2b. Model singleton

Replace `_ocr_engine()` with `_surya_models()`. Model loading is slow (~2–4s
first call, fast from cache thereafter):

```python
@lru_cache(maxsize=1)
def _surya_models():
    """Load Surya detection + recognition models (cached after first call)."""
    from surya.model.detection.model import (
        load_model as _ldm, load_processor as _ldp,
    )
    from surya.model.recognition.model import load_model as _lrm
    from surya.model.recognition.processor import load_processor as _lrp
    return _ldm(), _ldp(), _lrm(), _lrp()
```

Keep the `@lru_cache` pattern — same cold-start semantics as today.

### 2c. `_surya_to_cells` implementation

```python
def _surya_to_cells(results) -> list[_Cell]:
    """Adapt Surya run_ocr output to list[_Cell] (same contract as _ocr_to_cells)."""
    cells: list[_Cell] = []
    if not results:
        return cells
    for line in results[0].text_lines:   # results[0] = first (only) image
        try:
            x1, y1, x2, y2 = line.bbox
            xc = (x1 + x2) / 2.0
            yc = (y1 + y2) / 2.0
            t = str(line.text).strip()
            if t:
                cells.append(_Cell(x=xc, y=yc, text=t))
        except Exception:
            continue
    return cells
```

Same defensive `try/except` pattern as `_ocr_to_cells`. Same center-point
derivation (`mean` of bbox extremes).

### 2d. `_run_ocr` updated body

```python
def _run_ocr(img: np.ndarray, label: str = "ocr") -> list[_Cell]:
    """Run Surya OCR and return _Cell list."""
    if _DEBUG:
        dest = _debug_save(img, label)
        _log.debug("[OCR DEBUG] %s: image %dx%dpx -> %s", label, img.shape[1], img.shape[0], dest)

    if _FIDELITY_OCR_BACKEND == "rapidocr":
        # Escape hatch — remove after live validation (LT-1/LT-2)
        ocr = _ocr_engine()
        result, _ = ocr(img)
        cells = _ocr_to_cells(result or [])
    else:
        from surya.ocr import run_ocr as _surya_run
        det_model, det_processor, rec_model, rec_processor = _surya_models()
        surya_result = _surya_run(
            [Image.fromarray(img)], [["en"]],
            det_model, det_processor, rec_model, rec_processor,
        )
        cells = _surya_to_cells(surya_result)

    _log.debug("[OCR DEBUG] %s: %d detections", label, len(cells))
    for c in cells:
        _log.debug("  x=%6.0f  y=%6.0f  %r", c.x, c.y, c.text)
    return cells
```

### 2e. Escape-hatch env var

Add a module-level constant read once at import time:

```python
# Top of module, near other module-level constants
_FIDELITY_OCR_BACKEND: str = os.environ.get("FIDELITY_OCR_BACKEND", "surya")
```

Default is `"surya"` after Step 2 completes. During Step 1 (no-behavior-change
landing), default is `"rapidocr"` while tests are authored. The escape hatch
is removed in Step 3 (post live-validation cleanup).

### 2f. Dependency changes

| File | Change |
|---|---|
| `fidelity_rebalancer/pyproject.toml` | Add `surya-ocr` to `[project] dependencies`; remove `rapidocr-onnxruntime` in Step 3 only |
| `run.ps1` lines 65 & 70 | Add `surya` to core-import check (`import ..., surya`) so `run.ps1` reinstalls when the dep is missing; remove `rapidocr_onnxruntime` in Step 3 only |

**Note on model weights:** Surya downloads weights (~300 MB) on first run to
`~/.cache/surya/`. This is a one-time cost; subsequent runs use the cache.
No weights are committed to the repo.

---

## 3. Implementation plan

Three atomic steps, each independently reviewable. Steps 1 and 2 land as
separate commits (or PRs if preferred); Step 3 is a cleanup commit after live
evidence is captured.

### Step 1 — Add dependency + escape hatch (tests-green, no behavior change)

Goal: add Surya as a dep, build the escape hatch, expose `_surya_models()` and
`_surya_to_cells()` — but keep `_FIDELITY_OCR_BACKEND` defaulting to
`"rapidocr"` so behavior is identical to today.

- `pyproject.toml`: add `surya-ocr` alongside `rapidocr-onnxruntime` (both present until Step 3).
- `run.ps1`: add `surya` import check.
- `atp_ocr.py`: add `_FIDELITY_OCR_BACKEND = os.environ.get("FIDELITY_OCR_BACKEND", "rapidocr")`.
- `atp_ocr.py`: add `_surya_models()` with `@lru_cache`.
- `atp_ocr.py`: add `_surya_to_cells()`.
- `atp_ocr.py`: update `_run_ocr()` to branch on `_FIDELITY_OCR_BACKEND`.
- Write T-1, T-3, T-4 tests (see §4) — all pass with default `"rapidocr"`.
- Full test suite green.

### Step 2 — Flip default to Surya + validate

Goal: make Surya the default; ensure all automated tests pass; run T-5 perf
benchmark; produce the golden-image fixture (T-2) if an ATP screenshot is
available.

- `atp_ocr.py`: change `_FIDELITY_OCR_BACKEND` default from `"rapidocr"` to `"surya"`.
- Write T-2 golden-image test (or mark `@pytest.mark.skip(reason="requires OCR fixture")` if no sanitized screenshot yet).
- Run T-5 performance benchmark; record latency numbers in the PR description.
- Full suite green.
- **PR description must include:** T-5 results and confirmation of T-1/T-3/T-4 pass.

### Step 3 — Cleanup after live validation

Trigger: LT-1 or LT-2 live ATP session produces a `journal.jsonl` event trail
with correct `symbol` / `shares` / `side` values captured via OCR (proving
Surya is reading the Orders panel accurately in production).

- Remove the `"rapidocr"` branch from `_run_ocr()`.
- Remove `_ocr_engine()`, `_ocr_to_cells()`, `_FIDELITY_OCR_BACKEND`.
- Remove `rapidocr-onnxruntime` from `pyproject.toml`.
- Remove `rapidocr` from `run.ps1` import check.
- Delete T-4 (fallback test, now obsolete).
- Full suite green.

---

## 4. Test plan

Test file: `fidelity_rebalancer/tests/test_ocr_backend.py`  
Golden fixture dir: `fidelity_rebalancer/tests/fixtures/ocr_golden/`

### T-1 — `_surya_to_cells` contract (unit, automated, Step 1)

Verify the adapter converts a synthetic Surya-shaped result to `_Cell` list
with correct center coordinates and text extraction.

```python
from adapters.atp_ocr import _surya_to_cells, _Cell

class _FakeTextLine:
    def __init__(self, bbox, text):
        self.bbox = bbox
        self.text = text
        self.confidence = 0.99

class _FakeResult:
    def __init__(self, lines):
        self.text_lines = lines

def test_surya_to_cells_computes_center():
    result = [_FakeResult([_FakeTextLine([100, 20, 200, 40], "SYNTH1")])]
    cells = _surya_to_cells(result)
    assert len(cells) == 1
    assert cells[0].x == pytest.approx(150.0)
    assert cells[0].y == pytest.approx(30.0)
    assert cells[0].text == "SYNTH1"

def test_surya_to_cells_empty_input():
    assert _surya_to_cells([]) == []

def test_surya_to_cells_filters_blank_text():
    result = [_FakeResult([
        _FakeTextLine([0, 0, 10, 10], "   "),   # whitespace only
        _FakeTextLine([20, 0, 30, 10], "BUY"),
    ])]
    cells = _surya_to_cells(result)
    assert len(cells) == 1 and cells[0].text == "BUY"

def test_surya_to_cells_skips_malformed():
    class _Bad:
        bbox = None       # will raise on unpacking
        text = "oops"
    result = [_FakeResult([_Bad()])]
    cells = _surya_to_cells(result)   # must not raise
    assert cells == []
```

### T-2 — Golden-image accuracy regression (semi-automated, Step 2)

Requires a sanitized ATP screenshot. If none is available at authoring time,
mark the test `skip` and create the fixture during the first live session.

Fixture format: `tests/fixtures/ocr_golden/<panel>_<date>.png` + matching
`<panel>_<date>.json` with expected detections (synthetic data only — real
ticker symbols replaced with SYNTH1/SYNTH2/SYNTH3, account numbers zeroed).

```python
@pytest.mark.skipif(not golden_fixtures_exist(), reason="no OCR golden fixtures")
@pytest.mark.parametrize("fixture_pair", load_golden_pairs())
def test_orders_panel_cells(fixture_pair, monkeypatch):
    """Surya extracts the expected cell text from sanitized Orders panel screenshots."""
    monkeypatch.setenv("FIDELITY_OCR_BACKEND", "surya")
    import importlib, adapters.atp_ocr as m; importlib.reload(m)

    img = np.array(Image.open(fixture_pair["image"]))
    expected_texts = set(fixture_pair["expected_texts"])   # set of str

    cells = m._run_ocr(img, label="golden_test")
    found_texts = {c.text for c in cells}
    missing = expected_texts - found_texts
    assert not missing, f"Surya missed expected texts: {missing}"
```

Sanitization helper (one-time, manual before commit):
`scripts/sanitize_ocr_fixture.py` — accepts a PNG + symbol-replacement map,
blanks account/balance regions, saves to `tests/fixtures/ocr_golden/`.

### T-3 — `_cluster_rows` unchanged behavior (unit, automated, Step 1)

Row-clustering logic must be unaffected by the engine swap. These tests are
already backend-independent — they use raw `_Cell` lists.

```python
from adapters.atp_ocr import _Cell, _cluster_rows

def test_cluster_rows_groups_by_y_proximity():
    cells = [
        _Cell(100, 10, "A"), _Cell(200, 11, "B"),   # row 0
        _Cell(100, 30, "C"), _Cell(200, 29, "D"),   # row 1
    ]
    rows = _cluster_rows(cells, y_tol=8)
    assert len(rows) == 2
    assert {c.text for c in rows[0]} == {"A", "B"}
    assert {c.text for c in rows[1]} == {"C", "D"}

def test_cluster_rows_single_row():
    cells = [_Cell(10, 5, "X"), _Cell(50, 6, "Y"), _Cell(90, 4, "Z")]
    rows = _cluster_rows(cells, y_tol=8)
    assert len(rows) == 1
    assert len(rows[0]) == 3

def test_cluster_rows_empty():
    assert _cluster_rows([]) == []
```

### T-4 — Escape-hatch env var (unit, automated, Step 1; deleted in Step 3)

```python
def test_rapidocr_backend_env_var(monkeypatch):
    monkeypatch.setenv("FIDELITY_OCR_BACKEND", "rapidocr")
    import importlib, adapters.atp_ocr as m; importlib.reload(m)
    assert m._FIDELITY_OCR_BACKEND == "rapidocr"

def test_surya_backend_env_var(monkeypatch):
    monkeypatch.setenv("FIDELITY_OCR_BACKEND", "surya")
    import importlib, adapters.atp_ocr as m; importlib.reload(m)
    assert m._FIDELITY_OCR_BACKEND == "surya"
```

### T-5 — Performance benchmark (manual, pre-merge on Step 2 PR)

Captures the latency of both backends against the same fixture image. Not
in CI — run manually and paste results into the PR description.

```python
# scripts/ocr_benchmark.py
import os, time, numpy as np
from PIL import Image

IMAGE = "fidelity_rebalancer/tests/fixtures/ocr_golden/orders_panel.png"
N = 5

def bench(backend: str, img: np.ndarray) -> float:
    os.environ["FIDELITY_OCR_BACKEND"] = backend
    import importlib, adapters.atp_ocr as m; importlib.reload(m)
    # Warm-up
    m._run_ocr(img, label="warmup")
    t0 = time.perf_counter()
    for _ in range(N):
        m._run_ocr(img, label="bench")
    return (time.perf_counter() - t0) / N * 1000

img = np.array(Image.open(IMAGE))
print(f"RapidOCR: {bench('rapidocr', img):.0f} ms/call")
print(f"Surya:    {bench('surya',    img):.0f} ms/call")
```

**Pass criterion:** Surya ≤ 2× RapidOCR avg latency. If Surya is slower,
document the delta; it will be fully mitigated by C-12 (async offload) anyway.
If Surya is faster (likely on GPU), note that too.

---

## 5. Acceptance criteria

| # | Criterion | When |
|---|---|---|
| AC-1 | T-1, T-3, T-4 automated tests pass | Step 1 |
| AC-2 | Full suite green with default `"rapidocr"` | Step 1 |
| AC-3 | Full suite green with default `"surya"` | Step 2 |
| AC-4 | T-5 benchmark results in PR description; Surya ≤ 2× RapidOCR latency | Step 2 |
| AC-5 | T-2 golden test exists (or marked skip with fixture creation plan) | Step 2 |
| AC-6 | Live ATP session (LT-1 or LT-2) produces correct `journal.jsonl` with Surya active | Step 3 trigger |
| AC-7 | Cleanup: `_ocr_engine`, `_ocr_to_cells`, `_FIDELITY_OCR_BACKEND`, `rapidocr-onnxruntime` removed | Step 3 |
| AC-8 | No other adapter files modified in any step | All steps |
| AC-9 | `test_no_io_in_engine` purity gate stays green (OCR adapters are excluded from it) | All steps |

---

## 6. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Surya bbox format changes between versions | Low | Pin `surya-ocr>=X.Y,<X+1` in pyproject; check release notes before upgrades |
| Model weights too large for the machine | Low | Surya's CPU-mode weights are ~300 MB; first run downloads them — note in onboarding |
| Surya slower than 2× RapidOCR on ATP machine | Medium (no GPU) | C-12 async offload fully mitigates latency; accept the tradeoff for accuracy |
| Character accuracy worse on a specific ATP font/size | Low | T-2 golden test catches this; escape hatch provides instant rollback |
| torch version conflict with other deps | Low | Surya specifies its own torch range; check `pip check` after install |
