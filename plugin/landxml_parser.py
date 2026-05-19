"""Parse LandXML horizontal alignment geometry into plain Python dataclasses.

LandXML stores point coordinates as `Northing Easting [Elevation]` (the first
two values are N, E). Callers that target QGIS must swap to (Easting, Northing)
when emitting QgsPointXY — this module preserves the raw LandXML order in
(north, east) tuples so the swap happens in one place (geometry_builder).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

# Prefer defusedxml (XXE / billion-laughs protection) — QGIS ships it on
# most platforms. Fall back to the stdlib parser only when defusedxml is
# truly absent; LandXML files in this plugin come from surveys the user
# selected from disk, not an adversarial network source, so the fallback
# is acceptable. ``# nosec`` acknowledges the residual risk is intentional.
try:
    from defusedxml import ElementTree as ET  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — fallback path
    from xml.etree import ElementTree as ET  # nosec B405


NE = tuple[float, float]  # (north, east) — LandXML order


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _parse_point(text: str | None) -> NE:
    if text is None:
        raise ValueError("empty point element")
    parts = text.strip().split()
    if len(parts) < 2:
        raise ValueError(f"point needs at least 2 coords, got: {text!r}")
    return (float(parts[0]), float(parts[1]))


def _child(elem: ET.Element, name: str) -> ET.Element | None:
    for c in elem:
        if _strip_ns(c.tag) == name:
            return c
    return None


def _required_child(elem: ET.Element, name: str) -> ET.Element:
    c = _child(elem, name)
    if c is None:
        raise ValueError(f"<{_strip_ns(elem.tag)}> missing required <{name}>")
    return c


@dataclass
class LineSeg:
    start: NE
    end: NE
    length: float | None = None
    kind: str = "line"
    desc: str | None = None  # raw LandXML ``desc`` — often a design-speed tag
    status: str = ""


@dataclass
class CurveSeg:
    start: NE
    center: NE
    end: NE
    radius: float
    rot: str  # "cw" or "ccw"
    length: float | None = None
    kind: str = "curve"
    desc: str | None = None
    status: str = ""


@dataclass
class SpiralSeg:
    start: NE
    pi: NE | None
    end: NE
    length: float
    radius_start: float | None  # None / inf means infinite radius (straight)
    radius_end: float | None
    rot: str  # "cw" or "ccw"
    spi_type: str = "clothoid"
    kind: str = "spiral"
    desc: str | None = None
    status: str = ""


Segment = LineSeg | CurveSeg | SpiralSeg


@dataclass(frozen=True)
class StaEquation:
    """One ``<StaEquation>`` child of an ``<Alignment>``.

    LandXML semantics: ``sta_internal`` is the alignment's *continuous*
    station coordinate at the equation point (defaulting to ``sta_back``
    when the attribute is omitted). ``sta_back`` is the displayed station
    just before the equation; ``sta_ahead`` is the displayed station just
    after. A forward equation (``sta_ahead > sta_back``) introduces a gap
    in the displayed station axis; a back equation (``sta_ahead <
    sta_back``) creates an overlap where two displayed station values
    refer to a single internal point.
    """

    sta_back: float
    sta_ahead: float
    sta_internal: float


@dataclass
class PVI:
    """Point of Vertical Intersection — a vertex in the vertical alignment."""

    station: float
    elev: float
    kind: str = "pvi"


@dataclass
class VertCurve:
    """Vertical curve at a PVI — parabolic (most common) or circular."""

    station: float       # PVI / vertex station
    elev: float          # PVI / vertex elevation
    length: float        # curve length, m
    radius: float | None = None  # circular only; None → parabolic
    kind: str = "vertcurve"


ProfileItem = PVI | VertCurve


@dataclass
class ProfAlign:
    """One vertical alignment within a Profile (e.g. 'design', 'existing')."""

    name: str
    elements: list[ProfileItem] = field(default_factory=list)


@dataclass
class Profile:
    name: str
    alignments: list[ProfAlign] = field(default_factory=list)


@dataclass
class Alignment:
    name: str
    length: float | None
    sta_start: float | None
    segments: list[Segment] = field(default_factory=list)
    profile: Profile | None = None  # vertical profile (`<Profile>` child), if present
    # Best-effort track number lifted from <Feature name="...track..."> children
    # of <Alignment>; falls back to a 3-5 digit run found in the alignment name.
    track_number: str | None = None
    # Station equations (``<StaEquation>`` children), sorted by ``sta_internal``.
    # Empty when the alignment has continuous stationing.
    equations: list[StaEquation] = field(default_factory=list)


@dataclass
class LandXMLMetadata:
    """Header-level info needed for the Alignments table's source columns."""

    project_name: str | None = None
    landxml_version: str | None = None


@dataclass
class CrossSectionSample:
    """One ``<PntList3D>`` sample inside a ``<CrossSect>`` element."""

    alignment_name: str
    station: float
    offset: float
    elevation: float


_STATUS_KEYWORDS = re.compile(
    r"\b(new|exist(?:ing)?|demolish(?:ion)?|temporary|temp)\b", re.IGNORECASE
)
_STATUS_NORMALISE = {
    "existing": "exist", "demolition": "demolish", "temporary": "temp",
}


def _extract_status(elem: ET.Element, desc: str | None) -> str:
    """Best-effort segment status from <Feature> children or the desc attribute.

    Precedence: Feature name/code attribute wins; desc fallback used only when
    no matching Feature is found.
    """
    for child in elem:
        if _strip_ns(child.tag) != "Feature":
            continue
        for attr in ("name", "code", "value"):
            val = (child.attrib.get(attr) or "").lower().strip()
            m = _STATUS_KEYWORDS.search(val)
            if m:
                word = m.group(1).lower()
                return _STATUS_NORMALISE.get(word, word)
    if desc:
        m = _STATUS_KEYWORDS.search(desc)
        if m:
            word = m.group(1).lower()
            return _STATUS_NORMALISE.get(word, word)
    return ""


def _parse_radius(text: str | None) -> float | None:
    if text is None or text.strip() == "" or text.strip().lower() in ("inf", "infinity"):
        return None
    v = float(text)
    return None if v == 0 else v


def _parse_line(elem: ET.Element) -> LineSeg:
    start = _parse_point(_required_child(elem, "Start").text)
    end = _parse_point(_required_child(elem, "End").text)
    length_attr = elem.attrib.get("length")
    desc = elem.attrib.get("desc")
    return LineSeg(
        start=start,
        end=end,
        length=float(length_attr) if length_attr else None,
        desc=desc,
        status=_extract_status(elem, desc),
    )


def _parse_curve(elem: ET.Element) -> CurveSeg:
    start = _parse_point(_required_child(elem, "Start").text)
    center = _parse_point(_required_child(elem, "Center").text)
    end = _parse_point(_required_child(elem, "End").text)
    radius = float(elem.attrib.get("radius") or elem.attrib.get("crvType:radius") or "0")
    if radius == 0:
        # Derive from |Start - Center|
        dn = start[0] - center[0]
        de = start[1] - center[1]
        radius = (dn * dn + de * de) ** 0.5
    rot = (elem.attrib.get("rot") or "ccw").lower()
    length_attr = elem.attrib.get("length")
    desc = elem.attrib.get("desc")
    return CurveSeg(
        start=start,
        center=center,
        end=end,
        radius=radius,
        rot=rot,
        length=float(length_attr) if length_attr else None,
        desc=desc,
        status=_extract_status(elem, desc),
    )


def _parse_spiral(elem: ET.Element) -> SpiralSeg:
    start = _parse_point(_required_child(elem, "Start").text)
    end = _parse_point(_required_child(elem, "End").text)
    pi_node = _child(elem, "PI")
    pi = _parse_point(pi_node.text) if pi_node is not None else None
    length = float(elem.attrib["length"])
    rot = (elem.attrib.get("rot") or "ccw").lower()
    spi_type = (elem.attrib.get("spiType") or "clothoid").lower()
    rs = _parse_radius(elem.attrib.get("radiusStart"))
    re_ = _parse_radius(elem.attrib.get("radiusEnd"))
    desc = elem.attrib.get("desc")
    return SpiralSeg(
        start=start,
        pi=pi,
        end=end,
        length=length,
        radius_start=rs,
        radius_end=re_,
        rot=rot,
        spi_type=spi_type,
        desc=desc,
        status=_extract_status(elem, desc),
    )


_SEG_PARSERS = {"Line": _parse_line, "Curve": _parse_curve, "Spiral": _parse_spiral}


def _iter_coord_geom(coord_geom: ET.Element) -> Iterator[Segment]:
    for child in coord_geom:
        name = _strip_ns(child.tag)
        parser = _SEG_PARSERS.get(name)
        if parser is None:
            continue
        try:
            yield parser(child)
        except (ValueError, KeyError) as exc:
            # Skip malformed segment but keep going; surface via print for QGIS log.
            print(f"[align2qgis] skipping <{name}>: {exc}")


def _parse_profile_item(elem: ET.Element) -> ProfileItem | None:
    """Parse a ``<PVI>`` / ``<ParaCurve>`` / ``<CircCurve>`` / ``<UnsymParaCurve>``.

    LandXML 1.x stores ``station elevation`` as space-separated text inside
    the element. We trust the schema's documented order; exporters that
    swap it occasionally exist but are rare enough to warrant a separate
    workaround if a file actually fails.
    """
    name = _strip_ns(elem.tag)
    text = (elem.text or "").strip().split()
    if len(text) < 2:
        return None
    try:
        sta = float(text[0])
        elev = float(text[1])
    except ValueError:
        return None
    if name == "PVI":
        return PVI(station=sta, elev=elev)
    if name in ("ParaCurve", "CircCurve", "UnsymParaCurve"):
        try:
            length = float(elem.attrib.get("length") or 0.0)
        except ValueError:
            length = 0.0
        radius_attr = elem.attrib.get("radius")
        radius: float | None = None
        if radius_attr:
            try:
                radius = float(radius_attr)
            except ValueError:
                radius = None
        return VertCurve(station=sta, elev=elev, length=length, radius=radius)
    return None


def _parse_station_equations(alignment_elem: ET.Element) -> list[StaEquation]:
    """Collect ``<StaEquation>`` children, defaulting ``staInternal`` to ``staBack``.

    Equations are returned sorted by ``sta_internal`` so the downstream
    map-builder doesn't have to re-sort. LandXML places ``<StaEquation>``
    as a direct child of ``<Alignment>`` (occasionally inside
    ``<CoordGeom>``), so we accept both.
    """
    out: list[StaEquation] = []
    for child in alignment_elem.iter():
        if _strip_ns(child.tag) != "StaEquation":
            continue
        back_attr = child.attrib.get("staBack")
        ahead_attr = child.attrib.get("staAhead")
        if back_attr is None or ahead_attr is None:
            continue
        try:
            sta_back = float(back_attr)
            sta_ahead = float(ahead_attr)
        except ValueError:
            continue
        internal_attr = child.attrib.get("staInternal")
        try:
            sta_internal = float(internal_attr) if internal_attr else sta_back
        except ValueError:
            sta_internal = sta_back
        out.append(
            StaEquation(
                sta_back=sta_back, sta_ahead=sta_ahead, sta_internal=sta_internal
            )
        )
    out.sort(key=lambda e: e.sta_internal)
    return out


def _parse_profile(elem: ET.Element) -> Profile:
    profile = Profile(name=elem.attrib.get("name") or "", alignments=[])
    for prof_align_elem in elem:
        if _strip_ns(prof_align_elem.tag) != "ProfAlign":
            continue
        prof_align = ProfAlign(
            name=prof_align_elem.attrib.get("name") or "",
            elements=[],
        )
        for child in prof_align_elem:
            item = _parse_profile_item(child)
            if item is not None:
                prof_align.elements.append(item)
        if prof_align.elements:
            profile.alignments.append(prof_align)
    return profile


# Top-level LandXML elements the plugin recognises (used by inspect_landxml so
# the user can see what their file contains and what is / isn't imported yet).
_INSPECT_TAGS = (
    "Alignment",
    "Profile",
    "Surface",
    "CgPoint",
    "Parcel",
    "PlanFeature",
    "PipeNetwork",
    "Monument",
    "Roadway",
    "CrossSect",
    "CrossSectSurf",
    "CoordinateSystem",
    "Project",
    "Application",
)


def profile_samples(
    profile: Profile | None, step: float = 1.0
) -> list[tuple[float, float]]:
    """Densely sample a ``Profile`` to ``[(station, elev), …]``.

    PVI vertices are emitted directly. Vertical curves (``ParaCurve`` /
    ``CircCurve`` / ``UnsymParaCurve``) are evaluated along the parabola
    defined by their back- and ahead-tangent grades, then sampled at
    ``step`` metres so the dock's linear lookup is sub-millimetre accurate
    over typical rail / road grades.

    Math: inside a vertical curve, ``y(s) = y_BVC + g_in·s + ((g_out -
    g_in)/(2L))·s²`` where ``s`` is the along-curve distance from the
    BVC (begin vertical curve). The PVI's stated elevation is the
    theoretical vertex; ``y_BVC = elev_PVI - (L/2)·g_in`` so the curve
    joins the tangents tangentially.

    Circular vertical curves are approximated parametrically the same way
    — at the radii typical of rail/road verticals the parabolic fit and
    the true circular arc differ by millimetres over kilometres.
    """
    if profile is None:
        return []
    out: list[tuple[float, float]] = []
    for prof_align in profile.alignments:
        items = sorted(prof_align.elements, key=lambda x: x.station)
        n = len(items)
        if n == 0:
            continue
        for i, item in enumerate(items):
            L = getattr(item, "length", 0.0) or 0.0
            if L <= 0 or not isinstance(item, VertCurve):
                out.append((item.station, item.elev))
                continue

            g_back = (
                (items[i].elev - items[i - 1].elev)
                / (items[i].station - items[i - 1].station)
                if i > 0 and items[i].station > items[i - 1].station
                else 0.0
            )
            g_ahead = (
                (items[i + 1].elev - items[i].elev)
                / (items[i + 1].station - items[i].station)
                if i < n - 1 and items[i + 1].station > items[i].station
                else 0.0
            )

            sta_bvc = item.station - L / 2.0
            elev_bvc = item.elev - (L / 2.0) * g_back

            n_steps = max(2, int(L / max(step, 1e-3)))
            curvature_term = (g_ahead - g_back) / (2.0 * L)
            for k in range(n_steps + 1):
                s = (L / n_steps) * k
                sta = sta_bvc + s
                elev = elev_bvc + g_back * s + curvature_term * s * s
                out.append((sta, elev))
    # Dedup consecutive identical stations that arise when a tangent endpoint
    # coincides with a curve BVC/EVC; keep the curve-evaluated value.
    out.sort(key=lambda p: p[0])
    deduped: list[tuple[float, float]] = []
    for s, e in out:
        if deduped and abs(s - deduped[-1][0]) < 1e-6:
            deduped[-1] = (s, e)
        else:
            deduped.append((s, e))
    return deduped


def inspect_landxml(source: str | bytes) -> dict[str, int]:
    """Return ``{tag: count}`` for every LandXML element type of interest.

    Used to answer "what's in my file?" without running a full import.
    Tags are counted regardless of nesting depth so the count reflects the
    *occurrence* of the element, not just direct children of ``<LandXML>``.
    """
    if isinstance(source, (bytes, bytearray)):
        root = ET.fromstring(source)  # nosec B314 — defusedxml preferred, see import
    else:
        try:
            root = ET.parse(source).getroot()  # nosec B314
        except (OSError, FileNotFoundError):
            root = ET.fromstring(source)  # nosec B314
    wanted = set(_INSPECT_TAGS)
    counts: dict[str, int] = {tag: 0 for tag in _INSPECT_TAGS}
    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        if tag in wanted:
            counts[tag] += 1
    return counts


_TRACK_NUM_RE = re.compile(r"\b\d{3,5}\b")


def _extract_track_number(alignment_elem: ET.Element, alignment_name: str) -> str | None:
    """Best-effort track number lookup. <Feature name="…track…"> wins; else
    fall back to the first 3-5 digit run in the alignment name.
    """
    for child in alignment_elem:
        if _strip_ns(child.tag) != "Feature":
            continue
        feat_name = (child.attrib.get("name") or "").lower()
        if "track" not in feat_name:
            continue
        value = child.attrib.get("code") or child.attrib.get("value")
        if value:
            return value.strip()
    m = _TRACK_NUM_RE.search(alignment_name or "")
    return m.group(0) if m else None


def _root_from_source(source: str | bytes) -> ET.Element:
    if isinstance(source, (bytes, bytearray)):
        return ET.fromstring(source)  # nosec B314 — defusedxml preferred, see import
    try:
        return ET.parse(source).getroot()  # nosec B314
    except (OSError, FileNotFoundError):
        return ET.fromstring(source)  # nosec B314


def _parse_metadata(root: ET.Element) -> LandXMLMetadata:
    version = root.attrib.get("version") if _strip_ns(root.tag) == "LandXML" else None
    project_name: str | None = None
    for child in root:
        if _strip_ns(child.tag) == "Project":
            project_name = child.attrib.get("name") or None
            break
    return LandXMLMetadata(project_name=project_name, landxml_version=version)


def parse_alignments(source: str | bytes) -> list[Alignment]:
    """Parse a LandXML file path, bytes, or string and return alignments.

    Accepts both real file paths and in-memory XML for testability.
    """
    alignments, _ = parse_alignments_with_meta(source)
    return alignments


def parse_alignments_with_meta(
    source: str | bytes,
) -> tuple[list[Alignment], LandXMLMetadata]:
    """Same as :func:`parse_alignments` but also returns header metadata."""
    root = _root_from_source(source)
    meta = _parse_metadata(root)

    alignments: list[Alignment] = []
    for alignments_node in root.iter():
        if _strip_ns(alignments_node.tag) != "Alignment":
            continue
        name = alignments_node.attrib.get("name") or "Alignment"
        length_attr = alignments_node.attrib.get("length")
        sta_attr = alignments_node.attrib.get("staStart")
        coord_geom = _child(alignments_node, "CoordGeom")
        segments: list[Segment] = []
        if coord_geom is not None:
            segments = list(_iter_coord_geom(coord_geom))
        profile_elem = _child(alignments_node, "Profile")
        profile = _parse_profile(profile_elem) if profile_elem is not None else None
        equations = _parse_station_equations(alignments_node)
        alignments.append(
            Alignment(
                name=name,
                length=float(length_attr) if length_attr else None,
                sta_start=float(sta_attr) if sta_attr else None,
                segments=segments,
                profile=profile,
                track_number=_extract_track_number(alignments_node, name),
                equations=equations,
            )
        )
    return alignments, meta


def parse_cross_sections(source: str | bytes) -> list[CrossSectionSample]:
    """Parse ``<Alignment>/<CrossSects>/<CrossSect>`` and yield samples.

    A ``<CrossSect>`` may carry ``<PntList3D>`` text of triplets
    ``offset elevation [extra]``; when that's missing we still emit a single
    on-centreline sample so downstream code sees the station exists.
    """
    root = _root_from_source(source)
    out: list[CrossSectionSample] = []
    for alignments_node in root.iter():
        if _strip_ns(alignments_node.tag) != "Alignment":
            continue
        align_name = alignments_node.attrib.get("name") or "Alignment"
        for child in alignments_node:
            if _strip_ns(child.tag) != "CrossSects":
                continue
            for xs in child:
                if _strip_ns(xs.tag) != "CrossSect":
                    continue
                sta_attr = xs.attrib.get("sta") or xs.attrib.get("station")
                try:
                    station = float(sta_attr) if sta_attr else 0.0
                except ValueError:
                    continue
                pnt_list = _child(xs, "PntList3D")
                samples_added = 0
                if pnt_list is not None and pnt_list.text:
                    nums = pnt_list.text.split()
                    for i in range(0, len(nums) - 1, 2):
                        try:
                            off = float(nums[i])
                            elev = float(nums[i + 1])
                        except ValueError:
                            continue
                        out.append(CrossSectionSample(
                            alignment_name=align_name, station=station,
                            offset=off, elevation=elev,
                        ))
                        samples_added += 1
                if samples_added == 0:
                    out.append(CrossSectionSample(
                        alignment_name=align_name, station=station,
                        offset=0.0, elevation=0.0,
                    ))
    return out
