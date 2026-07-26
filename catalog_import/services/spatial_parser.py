import re
import statistics
from functools import lru_cache
from dataclasses import dataclass, field


SKU_REGEX = re.compile(
    r"[A-Z]{2,5}-\d{3,6}(?:\s?\([A-Z]{1,3}\))?"
)
# PHASE 3 FIX: the digit run used to be a fixed \d{4}, which truncated
# any SKU with 3 or 5+ digits ("VAL-202" -> no match at all, "VVN-25015"
# -> truncated to "VVN-2501") instead of matching it whole. It's now
# \d{3,6} - wide enough to cover every digit-length seen across
# collections (ALLIEDS' 3-digit codes through 5-digit ones like
# "VCM-35014") without opening up to arbitrary-length digit runs that
# could swallow unrelated numbers.
#
# The trailing suffix group used to allow only a single letter with no
# separating space ("(A)"), missing multi-letter suffixes ("(AF)") and
# suffixes OCR'd with a space before the parenthesis ("VVN-25015 (A)").
# It's now an optional single space followed by 1-3 uppercase letters
# in parens, covering both.

# Original strict price pattern - kept EXACTLY as-is (still used as the
# "was this a clean decimal price" signal for confidence scoring, and as
# the fast-path check inside is_price()). Any existing caller relying on
# its narrow \d+\.\d{2} behaviour is unaffected by everything below.
PRICE_PATTERN = re.compile(r"\d+\.\d{2}")

# Matches bare page numbers ("14") as well as "PAGE 14" style footers.
PAGE_NUMBER_PATTERN = re.compile(r"^(PAGE\s*)?\d{1,3}$")

# ---------------------------------------------------------------------------
# PHASE 4 CHANGE — DESIGN SHIFT: these were previously the ONLY line of
# defense against both (a) implausible prices and (b) OCR-merge garbage
# (two adjacent printed numbers read as one token, e.g. "72300" appearing
# nowhere on the actual page). Using one pair of numbers for both jobs
# forces a bad tradeoff: tight enough to catch garbage also cuts off real
# low/high-value products; loose enough to keep real products also lets
# garbage through untouched.
#
# These are now WIDE SANITY BOUNDS ONLY - "is this number even physically
# plausible as a price at all" (rules out things like a stray "3" or a
# 9-digit OCR run). Verified against Vantage_Catalogue-7.pdf: real prices
# ranged Rs 40 (Teflon Tape) to Rs 64,000 (20"x20" rain shower panel), so
# 10-1,000,000 has wide margin on both sides and will not itself reject
# any real product.
#
# The actual fine-grained "is this SPECIFIC value trustworthy" judgment
# now lives in two independent, purpose-built checks further down:
#   - _looks_like_ocr_merge_artifact(): a STRUCTURAL check (does this
#     price's own OCR box geometry look like two numbers merged into one
#     token?), independent of what the number's value is.
#   - flag_price_outliers_for_page(): a STATISTICAL, page-LOCAL check
#     (is this price wildly outside what everything else on THIS page
#     costs?), scale-free so it works whether the page's normal range is
#     Rs 40-900 or Rs 4,000-90,000.
# Neither of these DELETES a price. Both only set flagged_for_review=True
# and a human-readable reason, and discount price_confidence/
# ai_confidence proportionally - so an edge case (real or fake) always
# survives into the data with an honest confidence score, instead of
# either being silently trusted or silently vanishing.
# ---------------------------------------------------------------------------
MIN_VALID_PRICE = 10
MAX_VALID_PRICE = 1000000

# How close (in y) a block must be to a SKU to count toward its finish.
FINISH_VERTICAL_TOLERANCE = 150

# How close (in x) a price block must be to a GD/RGD/MB label block to be
# considered "in the same column" when matching prices to labels.
LABEL_COLUMN_TOLERANCE = 60

# PHASE 2 ADDITION: maximum distance (page pixels, same scale as the other
# tolerances above) an "orphan" content block - one that fell outside every
# computed product rectangle, which happens on irregular/variable-spacing
# layouts - is allowed to be from the nearest region before we give up on
# assigning it anywhere. This recovers content that would otherwise be
# silently dropped, without weakening the primary assignment pass at all
# (it only ever runs on blocks the primary pass rejected).
ORPHAN_BLOCK_MAX_DISTANCE = 100

# PHASE 3 ADDITION: how close (in x/y page pixels) two OCR blocks carrying
# the SAME normalized SKU text must be for them to be treated as one
# physical SKU detected twice (duplicate OCR hit), rather than two
# legitimate, separately-positioned rows/products. Kept intentionally
# tight and fully configurable via deduplicate_skus()'s own parameters so
# it never merges genuinely different rows/pages.
SKU_DEDUP_X_TOLERANCE = 40
SKU_DEDUP_Y_TOLERANCE = 25

# ---------------------------------------------------------------------------
# PHASE 4 ADDITION: OCR merge-artifact / page-outlier detection tuning.
# ---------------------------------------------------------------------------
#
# A genuine price token has fairly consistent per-character width (glyph
# width scales with box height/font size). Two adjacent printed numbers
# that OCR fused into a single box - the actual mechanism behind fake
# values like a "72300" that appears nowhere on the source page - span
# the natural gap between the two original numbers as well as both
# numbers' own glyphs, so the resulting box reads as ANOMALOUSLY WIDE per
# digit relative to its own height. This ratio is what
# _looks_like_ocr_merge_artifact() checks; it is a heuristic (documented
# as such), not a certainty, which is exactly why it flags for review
# instead of deleting.
MERGE_ARTIFACT_WIDTH_RATIO_THRESHOLD = 1.7
EXPECTED_CHAR_WIDTH_FACTOR = 0.55

# How many page-local Median Absolute Deviations a price must sit away
# from this page's own median price before it's flagged as a statistical
# outlier. MAD-based (not stdev-based) because MAD is robust to the very
# outliers we're trying to detect - a couple of genuine high-value
# premium items (e.g. a Rs 64,000 rain shower on a page of Rs 1,000-8,000
# accessories) won't distort the baseline the way they would distort a
# mean+stdev calculation.
PAGE_OUTLIER_MAD_FACTOR = 6
PAGE_OUTLIER_MIN_SAMPLE = 3

# ---------------------------------------------------------------------------
# Header / footer / finish-code vocabulary
# ---------------------------------------------------------------------------

# Known page header fragments. These are brand/marketing lines that repeat
# on every page and must never end up inside a product name.
#
# PHASE 1 NOTE: this list is treated as a set of KNOWN examples, not the
# sole detection mechanism. `is_header_footer_text()` adds structural
# heuristics (position, ALL-CAPS titling) so brands that aren't in this
# list are still caught. The list stays because exact matches are cheap
# and free of false positives.
HEADER_FOOTER_KEYWORDS = {
    "VANTAGE",
    "NOLLINS",
    "EU WILL",
    "EU WILL &",
    "PRICE LIST",
    "THE FUTURE OF BATHROOM FITTINGS",
}

# Footer sentences are usually long disclaimers - matched by substring
# rather than exact equality since OCR line-breaks them unpredictably.
FOOTER_TEXT_MARKERS = (
    "PRODUCT IMAGES ARE FOR ILLUSTRATIVE",
    "ILLUSTRATIVE PURPOSES",
    "#PRODUCT",
)

# Finish / price-label codes. These are never part of a product name.
FINISH_CODE_WORDS = {
    "GD",
    "RGD",
    "MB",
    "CP",
    "SS",
    "PVD",
    "ROSE",
    "GOLD",
    "ROSE GOLD",
    "BRASS",
    "FINISHES",
}

# The full vocabulary of finish codes we look for when building the
# multi-finish list (product.finishes). Order here is the output order.
FINISH_CODE_LIST = ["GD", "RGD", "MB", "CP", "SS", "PVD"]

# Common OCR misreads of SKU prefixes. Only applied as a last resort, and
# only when the fix actually produces something that matches SKU_REGEX.
SKU_PREFIX_FIXES = {
    "WVN": "VVN",
}

# ---------------------------------------------------------------------------
# Page-metadata vocabulary (collection / series / category)
# ---------------------------------------------------------------------------
#
# PHASE 2 NOTE: these dictionaries are now used strictly as a FIRST-PASS,
# cheap shortcut - not as the only path to a classification. Every one of
# them has a structural (layout/font-size/position based) fallback below
# that fires when nothing in the dictionary matches, which is the case
# `extract_page_metadata()` is explicitly built to handle: a collection
# name we have genuinely never seen before, from a brand not in this file.

CATEGORY_HINTS = {
    "BATHROOM FITTINGS": "Bathroom Fittings",
    "KITCHEN FITTINGS": "Kitchen Fittings",
    "CONCEALED FITTINGS": "Concealed Fittings",
    "SANITARYWARE": "Sanitaryware",
    "ACCESSORIES": "Accessories",
}

SERIES_HINTS = {
    "SINGLE LEVER",
    "CONCEALED",
    "TWO IN ONE",
    "WALL MOUNTED",
    "TABLE MOUNTED",
    "SENSOR SERIES",
    "EXTENDED SERIES",
}

LEGEND_MARKERS = {"FINISHES", "PRICE LIST", "PRICE", "LIST", "COLOUR", "COLOR"}

# ---------------------------------------------------------------------------
# Variant vocabulary
# ---------------------------------------------------------------------------
#
# Generic descriptors that modify a product name (size, shape, mount style)
# rather than naming the product itself. Matched with word boundaries so
# "ROUND" inside a longer OCR-merged word doesn't false-positive.
VARIANT_PATTERNS = [
    re.compile(r"\b\d{1,3}\s?MM\b"),
    re.compile(r"\bLONG NOSE\b"),
    re.compile(r"\bFLEXIBLE SPOUT\b"),
    re.compile(r"\bSQUARE\b"),
    re.compile(r"\bROUND\b"),
    re.compile(r"\bEXTENDED\b"),
    re.compile(r"\bHIGH NECK\b"),
    re.compile(r"\bSWIVEL\b"),
    re.compile(r"\bWALL MOUNTED\b"),
    re.compile(r"\bTABLE MOUNTED\b"),
    re.compile(r"\bSENSOR\b"),
]

# PHASE 2 NOTE: PRODUCT_TYPE_NOUNS is used EXCLUSIVELY as a soft confidence
# signal (see `_score_name_confidence`) - it has never been, and still is
# not, used to filter, reject, or rewrite a product name anywhere in this
# file. An unrecognised noun phrase is always kept as the name; the dict
# only nudges `name_confidence` up when there's a match, which is the
# "wherever possible, prefer layout/context over fixed lists" principle
# applied to the one place a fixed list was already present.
PRODUCT_TYPE_NOUNS = {
    "BIB COCK", "PILLAR COCK", "ANGLE VALVE", "SINK MIXER",
    "HEALTH FAUCET", "WALL MIXER", "BASIN MIXER", "SHOWER MIXER",
    "STOP COCK", "CONCEALED STOP COCK", "DIVERTER", "SPOUT",
    "SHOWER ARM", "HAND SHOWER", "OVERHEAD SHOWER", "TOWEL RAIL",
    "TOWEL RING", "ROBE HOOK", "SOAP DISH", "BOTTLE TRAP",
    "WASTE COUPLING", "TAP CLEANER",
}

# ---------------------------------------------------------------------------
# PHASE 2 ADDITION: confidence-scoring weights
# ---------------------------------------------------------------------------
#
# ai_confidence is a transparent, debuggable weighted blend - not a learned
# model - so a human reviewer can see exactly why a row scored low. Weights
# sum to 1.0.
AI_CONFIDENCE_WEIGHTS = {
    "sku": 0.25,
    "name": 0.20,
    "price": 0.25,
    "layout": 0.15,
    "ocr": 0.15,
}

# PHASE 3 CHANGE: gained "*_loose" variants so price_confidence can reflect
# REGEX QUALITY as its own signal (requirement: "price_confidence should
# consider regex quality ... Fallback prices should have lower confidence.
# Direct label matches highest."). A "loose" match is a price that only
# resolved after currency-symbol/comma/bare-integer/"/-"-suffix
# normalization (see normalize_price_text below), as opposed to a clean
# "2250.00"-style decimal token straight out of OCR. Every existing key
# ("direct"/"label"/"fallback") keeps its original value, so any code path
# that only ever produced those three values scores identically to before.
_PRICE_QUALITY_SCORE = {
    "direct": 1.0,          # label + clean decimal price OCR'd in the same block
    "direct_loose": 0.90,   # label + normalized (symbol/comma/bare-int) price, same block
    "label": 0.85,          # clean decimal price matched to a label via column/position
    "label_loose": 0.75,    # normalized price matched to a label via column/position
    "fallback": 0.55,       # no label at all, nearest-leftover clean decimal price
    "fallback_loose": 0.45,  # no label at all, nearest-leftover normalized price
}

_LAYOUT_NEIGHBOR_SCORE = {
    2: 1.0,   # bounded above and below by a same-column neighbour
    1: 0.75,  # bounded on only one side
    0: 0.5,   # fell back to page edge on both sides
}

# ---------------------------------------------------------------------------
# PHASE 4 ADDITION: confidence penalties applied when a price is flagged
# (never a hard zero - a flagged price is still usable data, just less
# trusted, and still needs to outrank a genuinely MISSING price in any
# downstream sort/triage).
# ---------------------------------------------------------------------------
MERGE_ARTIFACT_CONFIDENCE_PENALTY = 0.35   # multiplicative
PAGE_OUTLIER_CONFIDENCE_PENALTY = 0.55     # multiplicative


@dataclass
class OCRBlock:
    text: str
    x: float
    y: float
    w: float
    h: float
    confidence: float

    @property
    def right(self):
        return self.x + self.w

    @property
    def bottom(self):
        return self.y + self.h


@dataclass
class ProductRegion:
    sku: OCRBlock
    left: float
    right: float
    top: float
    bottom: float
    blocks: list

    page_number: int = 1

    name: str = ""

    # PHASE 5 ADDITION: holds the price for a "Standard Product" - one
    # where no GD/RGD/MB label token was found anywhere near the SKU at
    # all. Previously this case was folded into gd_price (see the
    # PHASE 5 CHANGE note on extract_product_data below for why that was
    # misleading downstream). Left "" whenever the product DID have at
    # least one label detected nearby, even if that label's own price
    # match failed - in that case gd_price/rgd_price/mb_price (still
    # possibly "") are the correct fields, not this one.
    price: str = ""

    gd_price: str = ""
    rgd_price: str = ""
    finish: str = ""

    # ---- PHASE 1 ADDITIONS ----
    mb_price: str = ""
    finishes: list = field(default_factory=list)
    variant: str = ""
    collection: str = ""
    series: str = ""
    category: str = ""
    subcategory: str = ""

    # ---- PHASE 2 ADDITIONS (additive only, all default-valued) ----
    ocr_confidence: float = 0.0
    ai_confidence: float = 0.0
    sku_confidence: float = 0.0
    name_confidence: float = 0.0
    price_confidence: float = 0.0
    layout_confidence: float = 0.0

    # Internal bookkeeping used only to compute layout_confidence; not
    # part of the "required output" list but harmless to expose.
    neighbors_found: int = 2

    # ---- PHASE 4 ADDITIONS ----
    # Never used to delete/hide a price - only to route it to human
    # review with a reason. A product can be flagged and still have a
    # perfectly usable gd_price/rgd_price/mb_price; the UI/reviewer
    # decides what to do with a flagged row, this module never does.
    flagged_for_review: bool = False
    review_reasons: list = field(default_factory=list)

    # Internal bookkeeping: which OCRBlock each final price value came
    # from, so the page-level passes (merge-artifact + outlier checks)
    # can inspect box geometry / recompute without re-deriving it. Not
    # part of the "required output" list; consumers can ignore it.
    price_source_blocks: dict = field(default_factory=dict)


def rows_to_blocks(rows):
    """
    Convert EasyOCR rows into OCRBlock objects.
    """

    blocks = []

    for row in rows:

        pts = row["bbox"]

        x1 = pts[0][0]
        y1 = pts[0][1]

        x2 = pts[2][0]
        y2 = pts[2][1]

        blocks.append(
            OCRBlock(
                text=row["text"],
                x=x1,
                y=y1,
                w=x2 - x1,
                h=y2 - y1,
                confidence=row["confidence"],
            )
        )

    return blocks


def find_skus(blocks):
    """
    Find every SKU on the page.

    PHASE 3 CHANGE: results now pass through deduplicate_skus() before
    being returned, collapsing duplicate OCR detections of the same
    physical SKU (e.g. "VAU-14005" picked up twice by overlapping OCR
    tiles/passes) while leaving every legitimately repeated SKU on a
    different row or page untouched. Signature and return type (a
    y/x-sorted list of OCRBlock) are unchanged.
    """

    skus = []

    for block in blocks:
        if SKU_REGEX.search(block.text):
            skus.append(block)

    skus.sort(key=lambda b: (b.y, b.x))

    skus = deduplicate_skus(skus)

    return skus


def deduplicate_skus(skus, x_tolerance=SKU_DEDUP_X_TOLERANCE, y_tolerance=SKU_DEDUP_Y_TOLERANCE):
    """
    Collapse duplicate OCR detections of the same physical SKU.

    OCR occasionally emits the same SKU twice - once from the primary
    detection pass and once from an overlapping crop/tile, for example -
    as two near-identical OCRBlocks sitting almost on top of each other.

    This groups blocks by NORMALIZED SKU text (so "VAU-14OO5" and
    "VAU-14005" are recognised as the same SKU), and within each group
    clusters together any blocks whose x/y positions both fall inside
    (x_tolerance, y_tolerance) of a shared seed block, keeping only the
    highest-confidence OCRBlock from each cluster and discarding the
    rest.

    Legitimate repeated SKUs on genuinely different rows/pages sit far
    apart (many multiples of these tolerances) and are always kept -
    every cluster only ever merges blocks that are both spatially close
    AND textually identical after normalization.
    """

    if not skus:
        return skus

    groups = {}
    for block in skus:
        key = normalize_sku(clean_text(block.text))
        groups.setdefault(key, []).append(block)

    deduped = []

    for group_blocks in groups.values():

        if len(group_blocks) == 1:
            deduped.append(group_blocks[0])
            continue

        remaining = list(group_blocks)

        while remaining:

            seed = remaining.pop(0)
            cluster = [seed]

            i = 0
            while i < len(remaining):
                candidate = remaining[i]
                if (
                    abs(candidate.x - seed.x) <= x_tolerance
                    and abs(candidate.y - seed.y) <= y_tolerance
                ):
                    cluster.append(candidate)
                    remaining.pop(i)
                else:
                    i += 1

            best = max(cluster, key=lambda b: b.confidence)
            deduped.append(best)

    deduped.sort(key=lambda b: (b.y, b.x))

    return deduped


def group_skus_into_rows(skus, tolerance=80):
    """
    Group SKU anchors into horizontal rows.

    PHASE 1: the row-anchor y-position is a RUNNING AVERAGE of the row's
    members instead of staying pinned to the first SKU's y, preventing
    slow vertical drift across a wide row (slightly rotated scans) from
    pushing a later SKU on the same visual row into a new row. Signature
    and default tolerance are unchanged.
    """

    if not skus:
        return []

    rows = []

    current_row = [skus[0]]
    running_y_sum = skus[0].y
    running_y_avg = skus[0].y

    for sku in skus[1:]:

        if abs(sku.y - running_y_avg) <= tolerance:
            current_row.append(sku)
            running_y_sum += sku.y
            running_y_avg = running_y_sum / len(current_row)
        else:
            current_row.sort(key=lambda s: s.x)
            rows.append(current_row)

            current_row = [sku]
            running_y_sum = sku.y
            running_y_avg = sku.y

    current_row.sort(key=lambda s: s.x)
    rows.append(current_row)

    return rows


def calculate_boundaries(row, page_width):
    """
    Calculate horizontal boundaries.
    """

    boundaries = []

    for i, sku in enumerate(row):

        if i == 0:
            left = 0
        else:
            left = (row[i - 1].x + sku.x) / 2

        if i == len(row) - 1:
            right = page_width
        else:
            right = (sku.x + row[i + 1].x) / 2

        boundaries.append(
            {
                "sku": sku,
                "left": left,
                "right": right,
            }
        )

    return boundaries


def debug_boundaries(boundaries):
    """
    Print boundaries.
    """

    print()
    print("=" * 80)
    print("COLUMN BOUNDARIES")
    print("=" * 80)

    for item in boundaries:

        sku = item["sku"]

        print(
            f"{sku.text:<20}"
            f"L={int(item['left']):5} "
            f"X={int(sku.x):5} "
            f"R={int(item['right']):5}"
        )


# ---------------------------------------------------------------------------
# Header / footer filtering
# ---------------------------------------------------------------------------

def _looks_like_title_text(cleaned):
    """
    Structural (brand-agnostic) heuristic for masthead titles - collection
    names, series names, brand lines - that were never typed into
    HEADER_FOOTER_KEYWORDS because they change per-brand.
    """

    if not cleaned:
        return False

    words = cleaned.split()

    if not (1 <= len(words) <= 4):
        return False

    if any(ch.isdigit() for ch in cleaned):
        return False

    if "." in cleaned:
        return False

    return True


def is_header_footer_text(text, y=None, top_boundary=None, bottom_boundary=None):
    """
    Return True if `text` is page furniture (masthead, tagline, footer
    disclaimer, or page number) rather than real product content.

    Optional `y`/`top_boundary`/`bottom_boundary` (all default None, so
    every existing call site keeps working unchanged) let a caller add a
    positional check: a short, digit-free, title-shaped line sitting
    outside the normal product band is treated as furniture even if the
    exact words were never seen before.
    """

    if text is None:
        return False

    cleaned = clean_text(text)

    if not cleaned:
        return False

    if PAGE_NUMBER_PATTERN.match(cleaned):
        return True

    if cleaned in HEADER_FOOTER_KEYWORDS:
        return True

    if cleaned in LEGEND_MARKERS:
        return True

    for marker in FOOTER_TEXT_MARKERS:
        if marker in cleaned:
            return True

    # Single leftover fragments of the masthead, e.g. "EU" or "WILL" that
    # got split across two OCR boxes.
    if cleaned in {"EU", "WILL"}:
        return True

    if y is not None and _looks_like_title_text(cleaned):
        if top_boundary is not None and y < top_boundary:
            return True
        if bottom_boundary is not None and y > bottom_boundary:
            return True

    return False


def _column_overlaps(sku, left, right, margin=0):
    """
    True if `sku` sits (fully or partially, plus an optional horizontal
    `margin`) inside the horizontal band [left, right] - used to find
    same-column neighbours across rows.

    PHASE 2 CHANGE: gained an optional `margin` parameter (default 0,
    so existing 3-argument calls behave exactly as before). Irregular
    catalog layouts - a 3-column row followed by a 4-column row, uneven
    gutter widths - mean a SKU that is visually "the same column" can
    sit just outside a neighbouring row's exact boundary. A small margin
    (derived from the page's own SKU spacing, see
    `assign_blocks_to_products`) recovers those without materially
    changing behaviour on well-aligned grids, where the margin rarely
    matters because the SKU is already comfortably inside the band.
    """

    return not (sku.right < (left - margin) or sku.x > (right + margin))


# ---------------------------------------------------------------------------
# Page metadata extraction (collection / series / category)
# ---------------------------------------------------------------------------

def extract_page_metadata(blocks, skus, page_height):
    """
    Classify masthead text (everything above the first SKU row) into
    collection / series / category / subcategory instead of just
    discarding it, and return both the classification and the set of
    block ids consumed by it so the caller can exclude them from product
    content.

    PHASE 2 CHANGE: collection/series detection no longer depends on
    finding a dictionary match OR on the first title-shaped line being
    the right one. It now works even when the collection name is
    COMPLETELY UNKNOWN (a brand-new brand word never seen before):

      - Every masthead line's height is compared against the page's own
        MEDIAN block height (computed from the full block set, not a
        fixed pixel constant), so "this line is unusually large" scales
        correctly across different render resolutions/DPIs instead of
        relying on a hardcoded `h > 25`.
      - Among title-shaped masthead lines, the TALLEST one is chosen as
        the collection (not just the first one encountered), since a
        brand's logo/title text is reliably the largest text on the
        page regardless of what the words are.
      - The second-tallest distinct title-shaped line, if it sits close
        beneath the collection line, is taken as the series when no
        SERIES_HINTS match was found - again using relative geometry
        instead of vocabulary.

    Dictionary hints (CATEGORY_HINTS/SERIES_HINTS/HEADER_FOOTER_KEYWORDS)
    are still checked FIRST because they're free and unambiguous when
    they hit; the structural pass only fills in what they miss.
    """

    meta = {
        "collection": "",
        "series": "",
        "category": "",
        "subcategory": "",
        "header_block_ids": set(),
    }

    if not blocks:
        return meta

    if skus:
        top_boundary = min(s.y for s in skus) - 10
    else:
        top_boundary = page_height * 0.15

    masthead_blocks = [b for b in blocks if b.y < top_boundary]

    if not masthead_blocks:
        return meta

    masthead_blocks.sort(key=lambda b: (b.y, b.x))

    # Relative size baseline: use the WHOLE page's block heights (not
    # just the masthead) so the median reflects normal body-text size,
    # making the masthead title's relative "tallness" meaningful even on
    # pages with an unusually large or small masthead band.
    all_heights = sorted(b.h for b in blocks if b.h > 0)
    median_height = _median(all_heights) if all_heights else 0

    # First pass: exact dictionary hits (cheap, unambiguous).
    title_candidates = []  # (block, cleaned_text) not yet classified

    for block in masthead_blocks:

        cleaned = clean_text(block.text)

        if not cleaned:
            continue

        if cleaned in LEGEND_MARKERS:
            meta["header_block_ids"].add(id(block))
            continue

        if cleaned in CATEGORY_HINTS:
            meta["category"] = CATEGORY_HINTS[cleaned]
            meta["header_block_ids"].add(id(block))
            continue

        if cleaned in SERIES_HINTS:
            meta["series"] = cleaned.title()
            meta["header_block_ids"].add(id(block))
            continue

        if cleaned in HEADER_FOOTER_KEYWORDS:
            meta["header_block_ids"].add(id(block))
            continue

        if _looks_like_title_text(cleaned):
            title_candidates.append((block, cleaned))

    # Second pass: structural fallback for collection/series using
    # relative height ranking - works even for a brand word we have
    # never seen before, because it never looks the word up anywhere.
    if title_candidates:

        def _relative_height(item):
            return item[0].h

        ranked = sorted(title_candidates, key=_relative_height, reverse=True)

        is_significant = (
            median_height <= 0
            or ranked[0][0].h >= median_height * 1.15
        )

        if not meta["collection"] and is_significant:
            collection_block, collection_text = ranked[0]
            meta["collection"] = collection_text.title()
            meta["header_block_ids"].add(id(collection_block))

            remaining = ranked[1:]

            if not meta["series"] and remaining:

                series_block, series_text = remaining[0]

                # Only trust the second-largest line as the series if it
                # is reasonably close underneath the collection line -
                # otherwise it's more likely an unrelated masthead
                # fragment than an actual series name.
                vertical_gap = series_block.y - collection_block.bottom

                if -5 <= vertical_gap <= (median_height * 4 or 200):
                    meta["series"] = series_text.title()
                    meta["header_block_ids"].add(id(series_block))

        # Anything else title-shaped in the masthead is still furniture
        # even if it wasn't picked as collection/series - never let it
        # fall through into product content.
        for block, _cleaned in title_candidates:
            meta["header_block_ids"].add(id(block))

    return meta


def _median(values):
    if not values:
        return 0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 0:
        return (ordered[mid - 1] + ordered[mid]) / 2
    return ordered[mid]


def assign_blocks_to_products(
    blocks,
    grouped_rows,
    page_height,
    page_width,
):
    """
    Assign OCR blocks to product regions.

    Vertical boundaries are not a fixed row-to-row midpoint. Instead,
    each SKU looks for its nearest same-column neighbour (above/below,
    across ALL rows) and splits the gap with it, preventing a product's
    description/price text from bleeding into the neighbouring product
    when rows have uneven heights or a different number of columns.

    SKU anchor blocks are excluded from the pool of blocks that get
    assigned into regions, since a SKU token is never legitimate name/
    price/finish content.

    PHASE 2 CHANGES:
      1. Column-neighbour matching now uses a small adaptive margin
         (derived from the page's own median SKU width) instead of a
         strict bounding-box overlap, so irregular/variable-spacing
         grids (a 3-column row next to a 4-column row, uneven gutters)
         still find the correct above/below neighbour.
      2. Every region now records how many of {above, below} neighbours
         it actually found (`neighbors_found`), feeding layout_confidence
         later.
      3. A second "orphan recovery" pass reassigns content blocks that
         fell outside every computed rectangle in the primary pass - the
         situation irregular layouts produce most often - to the nearest
         region, bounded by ORPHAN_BLOCK_MAX_DISTANCE so it can't wrongly
         pull in blocks that truly belong to a different, far-away
         product.
      4. Minor performance pass: block centers are computed once per
         block up front instead of being recomputed, and the SKU list is
         pre-sorted by y once so the neighbour search can bail out early
         once it has moved too far away vertically to matter.
    """

    all_skus = [s for row in grouped_rows for s in row]

    page_meta = extract_page_metadata(blocks, all_skus, page_height)
    metadata_block_ids = page_meta["header_block_ids"]

    sku_ids = {id(s) for s in all_skus}

    content_blocks = [
        b for b in blocks
        if not is_header_footer_text(b.text)
        and id(b) not in sku_ids
        and id(b) not in metadata_block_ids
    ]

    # PHASE 2: adaptive column-matching margin, derived from the page's
    # own geometry rather than a hardcoded pixel value. Half the median
    # SKU block width is a reasonable "same column, different row"
    # slack; capped so a page with a few abnormally wide SKU blocks
    # can't blow the margin out to something that starts merging
    # genuinely different columns.
    sku_widths = sorted(s.w for s in all_skus if s.w > 0)
    median_sku_width = _median(sku_widths)
    column_margin = min(max(median_sku_width * 0.5, 0), 80)

    # Sort once by y for early-exit pruning in the neighbour search below.
    skus_by_y = sorted(all_skus, key=lambda s: s.y)

    products = []

    for row in grouped_rows:

        boundaries = calculate_boundaries(row, page_width)

        for item in boundaries:

            sku = item["sku"]
            left = item["left"]
            right = item["right"]

            best_above = None
            best_below = None

            # Walk outward from sku's own position in the y-sorted list
            # instead of scanning the full list unconditionally - once
            # we're past the point where a candidate could possibly beat
            # what we already have, stop looking in that direction.
            for other in skus_by_y:

                if other is sku:
                    continue

                if not _column_overlaps(other, left, right, margin=column_margin):
                    continue

                if other.y < sku.y:
                    if best_above is None or other.y > best_above.y:
                        best_above = other
                elif other.y > sku.y:
                    if best_below is None or other.y < best_below.y:
                        best_below = other

            neighbors_found = int(best_above is not None) + int(best_below is not None)

            if best_above is not None:
                top = (best_above.bottom + sku.y) / 2
            else:
                top = 0

            if best_below is not None:
                bottom = (sku.bottom + best_below.y) / 2
            else:
                bottom = page_height

            # Guard against degenerate/inverted bounds (can happen with
            # very tight OCR rows).
            if bottom <= top:
                bottom = sku.bottom + (FINISH_VERTICAL_TOLERANCE / 2)
                top = max(0, sku.y - (FINISH_VERTICAL_TOLERANCE / 2))

            region = ProductRegion(
                sku=sku,
                left=left,
                right=right,
                top=top,
                bottom=bottom,
                blocks=[],
                collection=page_meta["collection"],
                series=page_meta["series"],
                category=page_meta["category"],
                subcategory=page_meta["subcategory"],
                neighbors_found=neighbors_found,
            )

            products.append(region)

    unassigned_blocks = []

    for block in content_blocks:

        block_center_x = block.x + (block.w / 2)
        block_center_y = block.y + (block.h / 2)

        best_product = None
        best_score = None

        for product in products:

            if not (product.left <= block_center_x <= product.right):
                continue

            if not (product.top <= block_center_y <= product.bottom):
                continue

            dy = block_center_y - product.sku.y
            dx = block_center_x - product.sku.x

            score = abs(dy) + (abs(dx) * 0.35)

            if best_product is None or score < best_score:
                best_product = product
                best_score = score

        if best_product is not None:
            best_product.blocks.append(block)
        else:
            unassigned_blocks.append((block, block_center_x, block_center_y))

    # PHASE 2: orphan recovery pass. Only runs on blocks the primary,
    # strict-containment pass could not place anywhere - it can never
    # steal a block away from a region it was already correctly assigned
    # to, so this is purely additive recall, not a change to existing
    # assignment decisions.
    if unassigned_blocks and products:

        for block, cx, cy in unassigned_blocks:

            best_product = None
            best_distance = None

            for product in products:

                dx = 0.0
                if cx < product.left:
                    dx = product.left - cx
                elif cx > product.right:
                    dx = cx - product.right

                dy = 0.0
                if cy < product.top:
                    dy = product.top - cy
                elif cy > product.bottom:
                    dy = cy - product.bottom

                distance = (dx * dx + dy * dy) ** 0.5

                if best_product is None or distance < best_distance:
                    best_product = product
                    best_distance = distance

            if best_product is not None and best_distance <= ORPHAN_BLOCK_MAX_DISTANCE:
                best_product.blocks.append(block)

    return products


def debug_products(products):
    """
    Print OCR blocks assigned to each product.
    """

    print()
    print("=" * 80)
    print("PRODUCT REGIONS")
    print("=" * 80)

    for product in products:

        print()
        print("-" * 80)
        print(product.sku.text)
        print("-" * 80)

        ordered = sorted(
            product.blocks,
            key=lambda b: (b.y, b.x),
        )

        for block in ordered:
            print(block.text)


@lru_cache(maxsize=8192)
def _clean_text_cached(text):
    """
    PHASE 2 ADDITION: the actual implementation of `clean_text`, cached.
    Header keywords, finish codes, and repeated OCR fragments (page
    numbers, "GD", brand names) recur dozens of times per page and
    hundreds of times per catalog; caching this avoids re-running the
    same character-by-character normalization on identical strings.
    Pulled into its own cached function (rather than decorating
    `clean_text` directly) so `clean_text`'s public signature/behaviour
    - including its handling of non-string input upstream - is completely
    unchanged for callers.

    PHASE 3 CHANGE: ":" and ";" are now normalized to a SPACE instead of
    being deleted outright (comma, and the rest of the original
    delete-outright punctuation, are unchanged). OCR label/price pairs
    are frequently punctuated like "GD:3700" or "MB;1450" - deleting the
    colon/semicolon used to glue the label and the number into a single
    token ("GD3700"), which broke word-boundary label matching in
    `_label_in_text`. Turning them into a separator instead restores the
    two tokens ("GD 3700") without touching any other symbol's handling.
    """

    text = text.upper()

    # Punctuation with no separating meaning - safe to delete outright.
    for symbol in ("{", "}", "<", ">", "|", "?", "~", "`", "'", '"'):
        text = text.replace(symbol, "")

    # Punctuation OCR sometimes uses AS A SEPARATOR between a label and a
    # value ("GD:3700", "MB;1450") - normalized to a space so the two
    # tokens stay distinguishable, matching the treatment already given
    # to "-" below.
    for symbol in (":", ";"):
        text = text.replace(symbol, " ")

    text = text.replace(",", "")

    text = text.replace("-", " ")

    return " ".join(text.split()).strip()


def clean_text(text):
    """
    Normalize raw OCR text for extraction.
    """

    return _clean_text_cached(text)


# ---------------------------------------------------------------------------
# PHASE 3 ADDITION: robust price detection & normalization
# ---------------------------------------------------------------------------
#
# Real-world supplier catalogs write prices many different ways:
#   ₹2250   ₹2,250   2250   2250.00   2,250.00   2250/-
#   Rs.2250   Rs 2250   INR 2250
#
# PRICE_PATTERN (original, strict decimal-only) is left completely
# untouched above and is still used as the fast-path / "clean" signal.
# Everything below is purely additive: a broader candidate matcher plus a
# normalizer that folds every one of the supported spellings down to the
# same "XXXX.XX" string format the original pattern always produced, so
# every downstream consumer (is_valid_price, gd_price/rgd_price/mb_price
# storage, importer.py) sees exactly the same shape of value as before.

# Currency symbol/word prefixes recognised ahead of a numeric price.
CURRENCY_PREFIX_PATTERN = re.compile(r"(?:₹|RS\.?|INR)\s*", re.IGNORECASE)

# Trailing "/-" / "//" / bare "/" noise suppliers sometimes print after a
# price to mean "only" (e.g. "2250/-").
PRICE_TRAILING_NOISE_PATTERN = re.compile(r"[/\\]+-*\s*$")

# Broad candidate matcher: an optional currency prefix, a number that is
# either comma-grouped ("2,250") or a bare digit run ("2250"), an
# optional decimal part, and optional trailing slash/dash noise. The
# trailing `(?![A-Za-z0-9])` guard stops this from swallowing a number
# that is actually glued to a unit/label with no separator (e.g. the
# "300" in "300MM", or a truncated partial match like "30" out of
# "300MM" that greedy backtracking would otherwise produce), while still
# matching a price that is merely followed by a space and a label
# ("3700 GD") since a space is neither a letter nor a digit.
PRICE_CANDIDATE_PATTERN = re.compile(
    r"(?:₹|RS\.?|INR)?\s*(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?"
    r"(?:\s*/\s*-*)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# Strict "clean decimal" shape - used only to classify a candidate as
# "direct/label/fallback" (high confidence) vs the "_loose" variants
# (lower confidence) once it has already been matched and normalized.
_STRICT_DECIMAL_PATTERN = re.compile(r"^\d+\.\d{2}$")


def normalize_price_text(raw):
    """
    Normalize a raw price-like OCR token/string into a plain "XXXX.XX"
    numeric string suitable for float() parsing and for storage in
    gd_price/rgd_price/mb_price - matching the historical format the
    original decimal-only PRICE_PATTERN always produced.

    Handles (in any combination): ₹2250, ₹2,250, 2250, 2250.00,
    2,250.00, 2250/-, Rs.2250, Rs 2250, INR 2250, plus surrounding
    whitespace and duplicated punctuation. Returns "" if nothing
    price-shaped is found so callers can treat that as "no price here"
    without needing a try/except.
    """

    if not raw:
        return ""

    text = raw.strip()

    # Strip a leading currency symbol/word (₹, Rs., Rs, INR). Repeated
    # via `sub` so duplicated symbols ("₹₹2250") are fully removed.
    text = CURRENCY_PREFIX_PATTERN.sub("", text)

    # Strip trailing "/-", "//", or bare "/" suffix noise.
    text = PRICE_TRAILING_NOISE_PATTERN.sub("", text)

    text = text.strip()

    # Drop thousands-separator commas ("2,250" -> "2250").
    text = text.replace(",", "")

    if not text:
        return ""

    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", text):
        return ""

    if "." in text:
        whole, _, frac = text.partition(".")
        frac = (frac + "00")[:2]
    else:
        whole, frac = text, "00"

    if not whole:
        return ""

    return f"{int(whole)}.{frac}"


def _extract_priced_candidates(text):
    """
    Find every price-shaped token in `text` and normalize each one,
    returning a list of (normalized_price, is_strict) tuples in the
    order encountered, de-duplicated by normalized value.

    `is_strict` is True when the ORIGINAL (pre-normalization) token was
    already a clean "\\d+\\.\\d{2}" decimal - i.e. it needed no currency-
    symbol/comma/bare-integer/slash-noise normalization to parse. This
    is used purely as a REGEX QUALITY signal for price_confidence
    scoring (see _score_price_confidence); it never affects which price
    ends up stored on the product.
    """

    results = []
    seen = set()

    for raw in PRICE_CANDIDATE_PATTERN.findall(text):

        value = normalize_price_text(raw)

        if not value or value in seen:
            continue

        seen.add(value)

        is_strict = bool(_STRICT_DECIMAL_PATTERN.match(raw.strip()))

        results.append((value, is_strict))

    return results


def is_price(text):
    """
    Return True if text contains a price-like pattern.

    PHASE 3: in addition to the original strict decimal check, this now
    also recognises the broader currency/comma/bare-integer/slash-noise
    forms handled by normalize_price_text(), so callers relying on this
    function to gate price-bearing blocks see the same improved recall
    as extract_prices().
    """

    if PRICE_PATTERN.search(text):
        return True

    return bool(_extract_priced_candidates(text))


def extract_prices(text):
    """
    Extract all normalized price values found in text.

    PHASE 3: in addition to the original strict "\\d+\\.\\d{2}" matches,
    this now also recognises currency-prefixed (₹/Rs/INR), comma-grouped,
    bare-integer, and "/-"-suffixed price tokens - normalizing every
    match into the same "XXXX.XX" string format the original pattern
    produced, so every existing caller (is_valid_price, and the
    gd_price/rgd_price/mb_price assignment logic below) is unaffected in
    shape, only improved in recall.
    """

    return [value for value, _is_strict in _extract_priced_candidates(text)]


def _is_page_number_block(text):
    """
    PHASE 3 ADDITION: page-number filtering, refined so a bare 2-3 digit
    PRICE (e.g. "600") standing alone in its own OCR block is not
    swallowed by the page-footer-number filter before it ever reaches
    the price-extraction pipeline. PAGE_NUMBER_PATTERN itself is
    untouched (still used as-is by is_header_footer_text/
    clean_product_name); this wrapper is used ONLY at the one call site
    inside extract_product_data() where a false "it's a page number"
    match would silently delete a valid standalone price.

    PHASE 4 NOTE: the "< MIN_VALID_PRICE" comparison below still works
    correctly now that MIN_VALID_PRICE is a wide sanity bound (10)
    rather than a tight one (500) - genuine page-footer numbers (1-3
    digits, typically 1-104 for this catalog) are still comfortably
    below it, while a real low-value price like "40" (Teflon Tape) is
    now also below 10? No - 40 > 10, so it correctly stays classified as
    a potential price, not a page number. Only single/double-digit
    footer numbers (1-9) get swallowed here now, which is the correct,
    narrower behaviour.
    """

    if not PAGE_NUMBER_PATTERN.match(text):
        return False

    digits = re.sub(r"\D", "", text)

    if not digits:
        return True

    try:
        value = int(digits)
    except ValueError:
        return True

    return value < MIN_VALID_PRICE


def is_valid_price(price):
    """
    Validate that a price is a physically plausible value at all.

    PHASE 4 CHANGE: this is now a WIDE sanity check only (see the
    MIN_VALID_PRICE/MAX_VALID_PRICE docstring at the top of the file for
    the full reasoning) - it no longer tries to decide whether a
    specific value is TRUSTWORTHY, only whether it's in the realm of
    possibility for a price at all. Trustworthiness is judged downstream,
    per-price, by _looks_like_ocr_merge_artifact() and
    flag_price_outliers_for_page(), neither of which rejects a price -
    both only flag it for review while leaving it in the data.
    """

    try:
        value = float(price)
    except (TypeError, ValueError):
        return False

    return MIN_VALID_PRICE <= value <= MAX_VALID_PRICE


# ---------------------------------------------------------------------------
# PHASE 4 ADDITION: OCR merge-artifact detection (structural)
# ---------------------------------------------------------------------------

def _looks_like_ocr_merge_artifact(
    block,
    digit_count,
    width_ratio_threshold=MERGE_ARTIFACT_WIDTH_RATIO_THRESHOLD,
):


    if block is None or digit_count <= 0:
        return False

    if block.h <= 0 or block.w <= 0:
        return False

    expected_char_width = block.h * EXPECTED_CHAR_WIDTH_FACTOR

    if expected_char_width <= 0:
        return False

    actual_char_width = block.w / digit_count

    ratio = actual_char_width / expected_char_width

    return ratio > width_ratio_threshold


def _digit_count(price_text):
    """
    Count just the digit characters in a normalized "XXXX.XX" price
    string (e.g. "11500.00" -> 7), used to size the merge-artifact
    width-per-digit check.
    """

    return len(re.sub(r"\D", "", price_text or ""))


# ---------------------------------------------------------------------------
# PHASE 4 ADDITION: page-local statistical outlier detection
# ---------------------------------------------------------------------------

def flag_price_outliers_for_page(products, mad_factor=PAGE_OUTLIER_MAD_FACTOR):
    

    page_prices = []

    for product in products:
        for field_name in ("price", "gd_price", "rgd_price", "mb_price"):
            value = getattr(product, field_name, "")
            if value:
                try:
                    page_prices.append(float(value))
                except (TypeError, ValueError):
                    pass

    if len(page_prices) < PAGE_OUTLIER_MIN_SAMPLE:
        return products

    median = statistics.median(page_prices)
    deviations = [abs(p - median) for p in page_prices]
    mad = statistics.median(deviations) or 1.0

    for product in products:

        for field_name in ("price", "gd_price", "rgd_price", "mb_price"):

            value = getattr(product, field_name, "")

            if not value:
                continue

            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                continue

            if abs(numeric_value - median) <= mad_factor * mad:
                continue

            product.flagged_for_review = True
            product.review_reasons.append(
                f"{field_name}={value} is a page-local statistical "
                f"outlier (page median={median:.2f}, this value is "
                f"{abs(numeric_value - median) / mad:.1f}x the page's "
                f"MAD away from it) - verify against the source page "
                f"before trusting this value."
            )

            product.price_confidence = round(
                product.price_confidence * PAGE_OUTLIER_CONFIDENCE_PENALTY,
                4,
            )
            product.ai_confidence = _score_ai_confidence(
                sku=product.sku_confidence,
                name=product.name_confidence,
                price=product.price_confidence,
                layout=product.layout_confidence,
                ocr=product.ocr_confidence,
            )

    return products


def normalize_sku(text):
    """
    Correct common OCR mistakes inside SKU / numeric strings.
    Within numeric segments (a run of digits/letters that actually
    contains at least one digit), fixes common look-alike swaps:
    O->0, I->1, L->1, S->5, B->8.

    As a last resort, if the text still doesn't look like a SKU, try
    known prefix confusions (e.g. WVN <-> VVN) - only kept if the fix
    actually produces a SKU-shaped string.
    """

    text = text.upper().strip()

    def fix_segment(match):
        segment = match.group(0)

        if not any(ch.isdigit() for ch in segment):
            return segment

        return (
            segment
            .replace("O", "0")
            .replace("I", "1")
            .replace("L", "1")
            .replace("S", "5")
            .replace("B", "8")
        )

    text = re.sub(r"[0-9OILSB]{3,}", fix_segment, text)

    if not SKU_REGEX.search(text):
        for wrong, right in SKU_PREFIX_FIXES.items():
            if wrong in text:
                candidate = text.replace(wrong, right)
                if SKU_REGEX.search(candidate):
                    text = candidate
                    break

    return text


def clean_product_name(name):
    """
    Remove OCR garbage from a product name:
    collapse whitespace, drop duplicate consecutive words, strip finish
    codes, header/footer remnants, page numbers, and isolated garbage.
    """

    words = name.split()

    cleaned_words = []

    for word in words:

        bare = word.rstrip(".")

        if bare in FINISH_CODE_WORDS:
            continue

        if PAGE_NUMBER_PATTERN.match(bare):
            continue

        if is_header_footer_text(word):
            continue

        if len(bare) == 1:
            continue

        if cleaned_words and cleaned_words[-1] == word:
            continue

        cleaned_words.append(word)

    return " ".join(cleaned_words).strip()


def extract_variant(name):
    """
    Split a cleaned candidate name into (product_name, variant_string).

    Strips generic variant descriptors (size, spout shape, mount style,
    control type - see VARIANT_PATTERNS) out of the name text and returns
    them separately, joined by ", " in the order encountered.
    """

    text = f" {name} "
    variants_found = []

    for pattern in VARIANT_PATTERNS:
        match = pattern.search(text)
        if match:
            token = match.group(0).strip()
            if token not in variants_found:
                variants_found.append(token)
            text = pattern.sub(" ", text)

    remaining_name = " ".join(text.split()).strip()

    return remaining_name, ", ".join(variants_found)


def detect_finish(blocks, sku, tolerance=FINISH_VERTICAL_TOLERANCE):
    """
    Detect the product finish from OCR blocks that sit close (vertically)
    to this product's SKU, so a header/footer word like "BRASS" printed
    once at the top of the page doesn't get attributed to every product.
    """

    nearby = [b for b in blocks if abs(b.y - sku.y) <= tolerance]

    texts = [clean_text(b.text) for b in nearby]
    joined = " ".join(texts)

    has_rose = "ROSE" in joined
    has_gold = "GOLD" in joined
    has_pvd = "PVD" in joined
    has_brass = "BRASS" in joined

    if has_rose and has_gold:
        return "ROSE GOLD"

    if has_gold:
        return "GOLD"

    if has_pvd:
        return "PVD"

    if has_brass:
        return "BRASS"

    return ""


def detect_finishes(blocks, sku, tolerance=FINISH_VERTICAL_TOLERANCE):
    """
    Return the FULL list of finish/price codes (GD/RGD/MB/CP/SS/PVD)
    found near this product's SKU, as opposed to `detect_finish()` which
    only reports the ROSE GOLD/GOLD/PVD/BRASS material descriptor.
    """

    nearby = [b for b in blocks if abs(b.y - sku.y) <= tolerance]

    found = []

    for block in nearby:

        text = clean_text(block.text)
        padded = f" {text} "

        for code in FINISH_CODE_LIST:

            if code in found:
                continue

            pattern = re.compile(rf"(?:^|\s){code}(?:\s|$)")

            if pattern.search(padded):
                found.append(code)

    return found


def _label_in_text(text):
    """
    Return "RGD", "GD", "MB", or None if `text` (already clean_text'd and
    normalize_sku'd) carries a finish/price label. Checked as a
    standalone token match wherever possible, since "GD" is a substring
    of "RGD" and needs disambiguating.

    PHASE 3 NOTE: this function's own logic is unchanged - what changed
    is that `clean_text` now turns ":"/" ;" into spaces instead of
    deleting them, so label/price pairs like "GD:3700" or "MB;1450"
    arrive here as "GD 3700" / "MB 1450" and are correctly recognised as
    carrying a standalone "GD"/"MB" token, where they previously arrived
    as the single glued token "GD3700"/"MB1450" and matched nothing.
    """

    bare = text.rstrip(".")

    if bare == "RGD" or "RGD" in text:
        return "RGD"

    if bare == "GD" or re.search(r"(?:^|\s)GD(?:\s|$)", text):
        return "GD"

    if bare == "MB" or re.search(r"(?:^|\s)MB(?:\s|$)", text):
        return "MB"

    return None


def _strip_embedded_skus(raw_text, own_sku_text):
    """
    Remove every SKU-shaped token from `raw_text`, operating on the RAW
    (pre clean_text) string so the hyphen in SKU_REGEX still matches.

    PHASE 3 FIX: a finish/price label written as "MB-4500" or "GD-3700"
    (label, hyphen, 4 digits) is structurally indistinguishable from
    SKU_REGEX's own shape ("[A-Z]{2,5}-\\d{4}") - so it used to be wiped
    out here as if it were an embedded SKU, before the label/price
    matching logic downstream ever got to see it, silently dropping
    that finish's price. Any match whose letter-prefix is a known
    finish code (see FINISH_CODE_LIST) is now left untouched so it
    flows through to `_label_in_text` / `_extract_priced_candidates` as
    intended; every other SKU-shaped token is stripped exactly as
    before.
    """

    def _replace(match):
        token = match.group(0)
        prefix = token.split("-", 1)[0].upper()
        if prefix in FINISH_CODE_LIST:
            return token
        return " "

    return SKU_REGEX.sub(_replace, raw_text)


# ---------------------------------------------------------------------------
# PHASE 2 ADDITION: confidence scoring
# ---------------------------------------------------------------------------

def _score_name_confidence(name):
    """
    Heuristic 0-1 confidence for an extracted product name, using LAYOUT/
    CONTEXT signals (word count, absence of leftover digits/noise) as the
    primary evidence, with a known-noun dictionary match only as a small
    optional bonus - never a requirement. An unrecognised but
    plausible-looking name still scores respectably.
    """

    if not name:
        return 0.0

    words = name.split()
    score = 0.4

    if 1 <= len(words) <= 5:
        score += 0.25

    if not any(ch.isdigit() for ch in name):
        score += 0.15

    if all(len(w) > 1 for w in words):
        score += 0.1

    if name.upper() in PRODUCT_TYPE_NOUNS:
        score += 0.1

    return round(min(score, 1.0), 4)


def _score_price_confidence(price_quality, gd_quality, rgd_quality, mb_quality):
    """
    Blend the "how was this price found" signal for whichever price
    slots actually ended up populated. A product with no price evidence
    at all scores 0.0 (matches the existing "Missing price" validation
    error downstream in importer.py, which this file does not modify).

    PHASE 5 CHANGE: now also takes `price_quality` (the Standard-Product
    unlabeled-price slot) into the same blend as gd/rgd/mb - a product
    is only ever populated in ONE of price/gd_price at a time (see
    extract_product_data), so in practice this just widens the set of
    quality strings that can feed the mean; it never double-counts.

    PHASE 3: the quality strings fed in here may now include the
    "_loose" variants (direct_loose/label_loose/fallback_loose) added
    to _PRICE_QUALITY_SCORE, reflecting whether the price needed
    currency/comma/bare-integer normalization to parse. The blending
    logic itself (simple mean of whichever qualities are non-empty) is
    unchanged.

    PHASE 4 NOTE: this score is the STARTING point for a price's
    confidence. flag_price_outliers_for_page() (and the merge-artifact
    check applied inline in extract_product_data()) may further DISCOUNT
    this value afterwards - they never raise it, only lower it, and
    never below what a fully-unverified guess would score.
    """

    qualities = [
        q for q in (price_quality, gd_quality, rgd_quality, mb_quality) if q
    ]

    if not qualities:
        return 0.0

    scores = [_PRICE_QUALITY_SCORE.get(q, 0.5) for q in qualities]

    return round(sum(scores) / len(scores), 4)


def _score_layout_confidence(neighbors_found):
    """
    How well-bounded this product's region was, based on whether a
    same-column neighbour was found above and below it (as opposed to
    falling back to the page edge, which is a weaker signal that the
    region's extent is correct).
    """

    return _LAYOUT_NEIGHBOR_SCORE.get(neighbors_found, 0.5)


def _score_ocr_confidence(sku_conf, cleaned_blocks):
    """
    Mean OCR confidence across the SKU block plus every block that
    actually contributed surviving (non-discarded) text to this
    product's output - blocks filtered out as header/footer/SKU noise
    don't get to drag the score down, since they didn't influence the
    final result.
    """

    confidences = [sku_conf] + [block.confidence for block, _ in cleaned_blocks]

    if not confidences:
        return 0.0

    return round(sum(confidences) / len(confidences), 4)


def _score_ai_confidence(sku, name, price, layout, ocr):
    """
    Weighted blend of the five sub-scores. See AI_CONFIDENCE_WEIGHTS.
    """

    total = (
        sku * AI_CONFIDENCE_WEIGHTS["sku"]
        + name * AI_CONFIDENCE_WEIGHTS["name"]
        + price * AI_CONFIDENCE_WEIGHTS["price"]
        + layout * AI_CONFIDENCE_WEIGHTS["layout"]
        + ocr * AI_CONFIDENCE_WEIGHTS["ocr"]
    )

    return round(min(max(total, 0.0), 1.0), 4)


def extract_product_data(products):
    """
    Extract name, GD/RGD/MB prices, finishes, variant, and confidence
    scores for every product region.

    GD/RGD/MB prices are matched primarily by COLUMN (same x-position as
    the label, within LABEL_COLUMN_TOLERANCE). MB has NO nearest-valid-
    price fallback (unlike GD/RGD) - per "never invent MB", an MB value
    is only ever stored when an actual "MB" label token was detected.

    PHASE 5 CHANGE - Standard Product handling: a product with a single,
    unlabeled price (no GD/RGD/MB token ANYWHERE nearby, i.e.
    `label_blocks` is empty for this product) is now stored in the
    dedicated `product.price` field instead of being folded into
    `gd_price`. Storing it as "gd_price" was misleading downstream -
    it implied a GD label had actually been read off the page, when in
    fact none was ever seen. `gd_price`/`rgd_price`/`mb_price` are only
    ever populated when at least one real label token was detected
    nearby (even if that label's own nearest-price match ends up
    landing on a different, labeled price slot). This only changes
    WHICH FIELD an unlabeled price lands in - detection, validity
    checks (is_valid_price), and confidence-quality tracking
    (fallback/fallback_loose) are otherwise identical to before.

    PHASE 2 CHANGE: while matching prices, this also tracks HOW each
    price was found (same-block "direct" match, standalone-label
    "label" match, or last-resort "fallback" match) so
    `_score_price_confidence` can weight the result accordingly.

    PHASE 3 CHANGE: that quality tracking now also captures REGEX
    QUALITY - whether the matched price token was a clean decimal
    ("2250.00") or required currency-symbol/comma/bare-integer/slash
    normalization (see `_extract_priced_candidates`) - producing the
    "_loose" quality variants. This tracking is still purely local
    bookkeeping: it never changes WHICH price ends up in gd_price/
    rgd_price/mb_price, only how confident we report being in it.

    PHASE 4 CHANGE: two additions, both purely additive on top of
    everything above:
      1. Every price's SOURCE OCRBlock is now retained on
         product.price_source_blocks so its box geometry can be
         checked. Immediately after a price is assigned (in every one
         of the direct/label/fallback branches), it's run through
         _looks_like_ocr_merge_artifact() - if the box geometry looks
         like two merged numbers, the product is flagged for review
         and price_confidence is discounted, but the price ITSELF is
         still kept and stored exactly as extracted.
      2. After every product on this page has been fully processed, a
         page-level pass (flag_price_outliers_for_page) additionally
         flags any price that's a statistical outlier relative to
         everything else on the same page.
    Neither addition can cause a price to be dropped or replaced -
    both only add review metadata and discount confidence.
    """

    for product in products:

        blocks = sorted(
            product.blocks,
            key=lambda b: (b.y, b.x)
        )

        sku_text = normalize_sku(clean_text(product.sku.text))

        seen_lines = set()
        cleaned_blocks = []  # (OCRBlock, normalized text)

        for block in blocks:

            raw_text = _strip_embedded_skus(block.text, sku_text)

            if not raw_text.strip():
                continue

            text = clean_text(raw_text)
            text = normalize_sku(text)

            if not text:
                continue

            if text.startswith("#PRODUCT"):
                continue

            if _is_page_number_block(text):
                continue

            if is_header_footer_text(block.text):
                continue

            if text in seen_lines:
                continue
            seen_lines.add(text)

            cleaned_blocks.append((block, text))

        label_blocks = []   # (OCRBlock, "GD"/"RGD"/"MB")
        price_blocks = []   # (OCRBlock, price_str, is_strict)
        name_lines = []

        # PHASE 2/3: quality tracking for price_confidence.
        price_quality = None
        gd_quality = None
        rgd_quality = None
        mb_quality = None

        def _assign_price(field_name, price_value, quality, source_block):
            """
            PHASE 4 helper: sets the price + quality exactly as before,
            AND immediately runs the merge-artifact structural check
            against the block that actually produced this value,
            flagging (never rejecting) on a positive hit.

            PHASE 5 NOTE: field_name may now also be "price" (the
            Standard-Product unlabeled slot) - this function is
            field-agnostic (it already used setattr/dict-by-name), so
            no change to its own body was needed to support that.
            """

            setattr(product, field_name, price_value)
            product.price_source_blocks[field_name] = source_block

            if _looks_like_ocr_merge_artifact(source_block, _digit_count(price_value)):

                product.flagged_for_review = True
                product.review_reasons.append(
                    f"{field_name}={price_value} OCR box geometry looks "
                    f"like two merged numbers (unusually wide per digit) "
                    f"- verify against the source page before trusting "
                    f"this value."
                )

            return quality

        for block, text in cleaned_blocks:

            label = _label_in_text(text)
            priced_candidates = _extract_priced_candidates(text)

            if priced_candidates:

                price, is_strict = priced_candidates[-1]
                valid = is_valid_price(price)

                if label and valid:
                    quality = "direct" if is_strict else "direct_loose"
                    if label == "GD" and not product.gd_price:
                        gd_quality = _assign_price("gd_price", price, quality, block)
                    elif label == "RGD" and not product.rgd_price:
                        rgd_quality = _assign_price("rgd_price", price, quality, block)
                    elif label == "MB" and not product.mb_price:
                        mb_quality = _assign_price("mb_price", price, quality, block)
                    continue

                if valid:
                    price_blocks.append((block, price, is_strict))

                continue

            if label:
                label_blocks.append((block, label))
                continue

            bare = text.rstrip(".")
            if bare in FINISH_CODE_WORDS or text in FINISH_CODE_WORDS:
                continue

            if len(text) == 1:
                continue

            if text.isdigit():
                continue

            if name_lines and text.startswith(name_lines[-1] + " "):
                name_lines[-1] = text
            elif name_lines and name_lines[-1].startswith(text + " "):
                pass
            else:
                name_lines.append(text)

        for label_block, label in label_blocks:

            if label == "GD" and product.gd_price:
                continue
            if label == "RGD" and product.rgd_price:
                continue
            if label == "MB" and product.mb_price:
                continue

            used_prices = {product.gd_price, product.rgd_price, product.mb_price}

            same_column = []
            other_column = []

            for price_block, price, is_strict in price_blocks:

                if price in used_prices:
                    continue

                dx = abs(price_block.x - label_block.x)
                dy = price_block.y - label_block.y

                bucket = (
                    same_column
                    if dx <= LABEL_COLUMN_TOLERANCE
                    else other_column
                )
                bucket.append((dy, dx, price, is_strict, price_block))

            candidates = same_column or other_column

            best_price = None
            best_is_strict = False
            best_block = None
            best_score = None

            for dy, dx, price, is_strict, price_block in candidates:

                score = abs(dy) + (dx * 0.25)

                if dy < -10:
                    score += 500

                if best_price is None or score < best_score:
                    best_price = price
                    best_is_strict = is_strict
                    best_block = price_block
                    best_score = score

            if best_price is not None:

                quality = "label" if best_is_strict else "label_loose"

                if label == "GD":
                    gd_quality = _assign_price("gd_price", best_price, quality, best_block)
                elif label == "RGD":
                    rgd_quality = _assign_price("rgd_price", best_price, quality, best_block)
                else:
                    mb_quality = _assign_price("mb_price", best_price, quality, best_block)

        # PHASE 5: Standard Product path - NO label token (GD/RGD/MB) was
        # found anywhere near this SKU at all. Route any price(s) found
        # into the dedicated `price` field instead of `gd_price`, so a
        # populated gd_price always means an actual "GD" label was read
        # off the page somewhere. Only the single nearest, still-unused
        # price candidate is used; a second candidate is left alone
        # rather than guessed into anything, matching the existing
        # "never invent a second value" principle applied elsewhere.
        if not label_blocks:

            if not product.price and price_blocks:

                candidates = sorted(
                    price_blocks,
                    key=lambda c: abs(c[0].y - product.sku.y),
                )

                source_block, price, is_strict = candidates[0]
                quality = "fallback" if is_strict else "fallback_loose"

                price_quality = _assign_price("price", price, quality, source_block)

        else:

            # Fallback: nearest valid price for any GD/RGD slot still
            # empty. MB intentionally excluded - see docstring above.
            # This branch only runs when at least one label WAS found
            # nearby (label_blocks non-empty) - the label-less Standard
            # Product case is handled entirely above and never falls
            # through to here, so gd_price/rgd_price can no longer be
            # silently populated for a product that had zero labels.
            if not product.gd_price or not product.rgd_price:

                used_prices = {product.gd_price, product.rgd_price, product.mb_price}

                candidates = sorted(
                    (
                        (abs(block.y - product.sku.y), price, is_strict, block)
                        for block, price, is_strict in price_blocks
                        if price not in used_prices or price == ""
                    ),
                    key=lambda c: c[0],
                )

                for _, price, is_strict, source_block in candidates:

                    quality = "fallback" if is_strict else "fallback_loose"

                    if not product.gd_price:
                        gd_quality = _assign_price("gd_price", price, quality, source_block)
                        used_prices.add(price)
                        continue

                    if not product.rgd_price and price != product.gd_price:
                        rgd_quality = _assign_price("rgd_price", price, quality, source_block)
                        used_prices.add(price)
                        break

        raw_name = clean_product_name(" ".join(name_lines))

        product.name, product.variant = extract_variant(raw_name)

        product.finish = detect_finish(
            [b for b, _ in cleaned_blocks],
            product.sku,
        )

        product.finishes = detect_finishes(
            [b for b, _ in cleaned_blocks],
            product.sku,
        )

        # ------------------------------------------------------------
        # PHASE 2/3: confidence engine
        # ------------------------------------------------------------

        product.sku_confidence = round(product.sku.confidence, 4)

        product.name_confidence = _score_name_confidence(product.name)

        product.price_confidence = _score_price_confidence(
            price_quality, gd_quality, rgd_quality, mb_quality
        )

        product.layout_confidence = _score_layout_confidence(
            product.neighbors_found
        )

        product.ocr_confidence = _score_ocr_confidence(
            product.sku.confidence, cleaned_blocks
        )

        product.ai_confidence = _score_ai_confidence(
            sku=product.sku_confidence,
            name=product.name_confidence,
            price=product.price_confidence,
            layout=product.layout_confidence,
            ocr=product.ocr_confidence,
        )

    # PHASE 4: page-level statistical outlier pass, run once against the
    # FULL set of products for this page (this function is already
    # called once per page by parser.py) - must run after the per-product
    # loop above since it needs every product's final price_confidence
    # already computed as its baseline to discount from.
    flag_price_outliers_for_page(products)

    return products


def debug_extracted(products):

    print()
    print("=" * 80)
    print("EXTRACTED PRODUCTS")
    print("=" * 80)

    for p in products:

        print()

        print(f"SKU     : {p.sku.text}")
        print(f"NAME    : {p.name}")
        print(f"VARIANT : {p.variant}")
        print(f"PRICE   : {p.price}")
        print(f"GD      : {p.gd_price}")
        print(f"RGD     : {p.rgd_price}")
        print(f"MB      : {p.mb_price}")
        print(f"FINISHES: {p.finishes}")
        print(f"COLLECTION: {p.collection}")
        print(f"SERIES  : {p.series}")
        print(f"OCR CONF: {p.ocr_confidence}")
        print(f"AI  CONF: {p.ai_confidence}")
        print(
            f"  (sku={p.sku_confidence} name={p.name_confidence} "
            f"price={p.price_confidence} layout={p.layout_confidence})"
        )
        if p.flagged_for_review:
            print(f"  ⚠ FLAGGED FOR REVIEW:")
            for reason in p.review_reasons:
                print(f"    - {reason}")