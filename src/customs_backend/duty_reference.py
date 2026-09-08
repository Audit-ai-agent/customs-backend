"""Full duty-rate reference store built from Korea Customs open data.

When ``CUSTOMS_BACKEND_DUTY_REFERENCE_DB`` points at a SQLite file produced
by ``tools/build_duty_reference.py``, the duty assessment serves the real
관세청 품목번호별 관세율표 (every HSK 10-digit code, every 관세율구분).
Without the env the packaged JSON snapshot keeps working — dev-fixture and
tests are unaffected.

Interpretation rules are deliberately conservative: a rate row participates
in "적용 가능" selection only when its meaning is certain (기본세율 A, WTO
양허 C, and FTA rows whose origin/certificate requirements are satisfied;
curated-membership preferences such as APTA/최빈국/북한산 only when the
origin country is in the curated list).  Everything else is displayed as a
conditional reference row and never silently applied.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_FTA_PORTAL = "https://www.customs.go.kr/ftaportalkor/main.do"
# CLIP cannot deep-link a specific HS code (POST/session-only navigation);
# the closest stable target is the Korea tariff-table search screen.
_UNIPASS = "https://unipass.customs.go.kr/clip/index.do?opnurl=%2Fhsinfosrch%2FopenULS0201002Q.do%3FcntyCd%3DKR"

_ASEAN = ("BN", "KH", "ID", "LA", "MY", "MM", "PH", "SG", "TH", "VN")
_EU27 = (
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
    "SI", "ES", "SE",
)
_EFTA = ("CH", "IS", "NO", "LI")
_CAM5 = ("CR", "SV", "HN", "NI", "PA")
_APTA = ("BD", "CN", "IN", "LK", "LA", "MN")
# UN list of least developed countries (최빈개도국 특혜관세 대상, curated).
_LDC = (
    "AF", "AO", "BD", "BJ", "BF", "BI", "KH", "CF", "TD", "KM", "CD", "DJ",
    "ER", "ET", "GM", "GN", "GW", "HT", "KI", "LA", "LS", "LR", "MG", "MW",
    "ML", "MR", "MZ", "MM", "NP", "NE", "RW", "ST", "SN", "SL", "SB", "SO",
    "SS", "SD", "TL", "TG", "TV", "TZ", "UG", "YE", "ZM",
)

#: agreement code → (Korean name, member countries, certificate form)
AGREEMENTS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "KR-CL": ("한-칠레 FTA", ("CL",), "원산지증명서 (한-칠레 서식)"),
    "KR-SG": ("한-싱가포르 FTA", ("SG",), "원산지증명서 (한-싱가포르 서식)"),
    "KR-EFTA": ("한-EFTA FTA", _EFTA, "원산지신고서 (인보이스 신고 방식)"),
    "KR-ASEAN": ("한-아세안 FTA", _ASEAN, "원산지증명서 (AK Form)"),
    "KR-IN": ("한-인도 CEPA", ("IN",), "원산지증명서 (한-인도 서식)"),
    "KR-EU": ("한-EU FTA", _EU27, "원산지신고서 (인보이스 신고 방식)"),
    "KR-PE": ("한-페루 FTA", ("PE",), "원산지증명서 (한-페루 서식)"),
    "KR-US": ("한-미 FTA", ("US",), "원산지증명서 (자율 발급)"),
    "KR-TR": ("한-튀르키예 FTA", ("TR",), "원산지신고서 (인보이스 신고 방식)"),
    "KR-CO": ("한-콜롬비아 FTA", ("CO",), "원산지증명서 (자율 발급)"),
    "KR-AU": ("한-호주 FTA", ("AU",), "원산지증명서 (자율/기관 발급)"),
    "KR-CA": ("한-캐나다 FTA", ("CA",), "원산지증명서 (자율 발급)"),
    "KR-CN": ("한-중국 FTA", ("CN",), "원산지증명서 (한-중 FTA 서식)"),
    "KR-NZ": ("한-뉴질랜드 FTA", ("NZ",), "원산지증명서 (자율 발급)"),
    "KR-VN": ("한-베트남 FTA", ("VN",), "원산지증명서 (KV Form)"),
    "KR-CAM": ("한-중미 FTA", _CAM5, "원산지증명서 (자율 발급)"),
    "KR-GB": ("한-영 FTA", ("GB",), "원산지신고서 (인보이스 신고 방식)"),
    "KR-ID": ("한-인도네시아 CEPA", ("ID",), "원산지증명서 (기관 발급)"),
    "KR-IL": ("한-이스라엘 FTA", ("IL",), "원산지증명서 (자율 발급)"),
    "KR-KH": ("한-캄보디아 FTA", ("KH",), "원산지증명서 (기관 발급)"),
    "KR-PH": ("한-필리핀 FTA", ("PH",), "원산지증명서 (기관 발급)"),
    "RCEP": ("역내포괄적경제동반자협정(RCEP)", _ASEAN + ("AU", "CN", "JP", "NZ"), "원산지증명서 (RCEP 서식)"),
}

#: FTA 세율구분코드 family prefix → (agreement code, country restriction).
#: Ordered longest-prefix-first at lookup time; suffix digits are staging
#: variants within the same agreement.
_FTA_FAMILIES: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "FASID": ("KR-ASEAN", ("ID",)),
    "FASPH": ("KR-ASEAN", ("PH",)),
    "FASTH": ("KR-ASEAN", ("TH",)),
    "FAS": ("KR-ASEAN", None),
    "FRCAS": ("RCEP", _ASEAN),
    "FRCAU": ("RCEP", ("AU",)),
    "FRCCN": ("RCEP", ("CN",)),
    "FRCJP": ("RCEP", ("JP",)),
    "FRCNZ": ("RCEP", ("NZ",)),
    "FCECR": ("KR-CAM", ("CR",)),
    "FCEHN": ("KR-CAM", ("HN",)),
    "FCENI": ("KR-CAM", ("NI",)),
    "FCEPA": ("KR-CAM", ("PA",)),
    "FCESV": ("KR-CAM", ("SV",)),
    "FEFCH": ("KR-EFTA", ("CH", "LI")),
    "FEFIS": ("KR-EFTA", ("IS",)),
    "FEFNO": ("KR-EFTA", ("NO",)),
    "FEF": ("KR-EFTA", None),
    "FAU": ("KR-AU", None),
    "FCA": ("KR-CA", None),
    "FCL": ("KR-CL", None),
    "FCN": ("KR-CN", None),
    "FCO": ("KR-CO", None),
    "FEU": ("KR-EU", None),
    "FGB": ("KR-GB", None),
    "FID": ("KR-ID", None),
    "FIL": ("KR-IL", None),
    "FIN": ("KR-IN", None),
    "FKH": ("KR-KH", None),
    "FNZ": ("KR-NZ", None),
    "FPE": ("KR-PE", None),
    "FPH": ("KR-PH", None),
    "FSG": ("KR-SG", None),
    "FTR": ("KR-TR", None),
    "FUS": ("KR-US", None),
    "FVN": ("KR-VN", None),
}
_FTA_PREFIXES = sorted(_FTA_FAMILIES, key=len, reverse=True)

#: Non-FTA families whose semantics we assert.  kind category:
#:  "basic"        — 기본세율, always applicable
#:  "wto"          — WTO 양허세율, applicable as reference MFN rate
#:  "preferential" — origin-conditional preference (curated membership)
#:  "conditional"  — quota/safeguard/other; displayed, never auto-applied
_NONFTA_FAMILIES: dict[str, tuple[str, str, tuple[str, ...] | None]] = {
    "A": ("basic", "기본세율", None),
    "C": ("wto", "WTO 양허세율", None),
    "D": ("preferential", "GATT 개도국간 양허세율", None),
    "E1": ("preferential", "아시아·태평양무역협정(APTA) 양허세율", _APTA),
    "E2": ("preferential", "APTA 양허세율(방글라데시)", ("BD",)),
    "E3": ("preferential", "APTA 양허세율(최빈개도국)", ("BD", "LA")),
    "G": ("preferential", "개도국간 특혜무역제도(GSTP) 양허세율", None),
    "R": ("preferential", "최빈개도국 특혜관세", _LDC),
    "U": ("preferential", "북한산 반입물품 세율", ("KP",)),
    "P": ("conditional", "할당관세", None),
    "L": ("conditional", "조정관세", None),
    "W": ("conditional", "시장접근물량 양허세율", None),
    "T": ("conditional", "양허세율(시장접근물량 관련)", None),
}
_NONFTA_PREFIXES = sorted(_NONFTA_FAMILIES, key=len, reverse=True)


@dataclass(frozen=True)
class KindSpec:
    family: str
    category: str  # basic | wto | preferential | fta | conditional
    label: str
    agreement_code: str | None
    countries: tuple[str, ...] | None  # None → membership unknown/global
    is_variant: bool


def classify_kind(kind: str) -> KindSpec:
    """Map one 관세율구분코드 to its curated interpretation."""
    for prefix in _FTA_PREFIXES:
        if kind.startswith(prefix):
            agreement, restriction = _FTA_FAMILIES[prefix]
            name, members, _form = AGREEMENTS[agreement]
            countries = restriction if restriction is not None else members
            suffix = kind[len(prefix):]
            base = f"{name} 협정세율"
            if restriction is not None and len(restriction) <= 2:
                base += f" ({'/'.join(restriction)})"
            return KindSpec(
                family=prefix,
                category="fta",
                label=base,
                agreement_code=agreement,
                countries=countries,
                is_variant=bool(suffix) and suffix not in ("", "1"),
            )
    for prefix in _NONFTA_PREFIXES:
        if kind.startswith(prefix):
            category, label, family_countries = _NONFTA_FAMILIES[prefix]
            suffix = kind[len(prefix):]
            return KindSpec(
                family=prefix,
                category=category,
                label=label,
                agreement_code=None,
                countries=family_countries,
                is_variant=bool(suffix),
            )
    return KindSpec(
        family=kind,
        category="conditional",
        label=f"세율구분 {kind}",
        agreement_code=None,
        countries=None,
        is_variant=False,
    )


@dataclass(frozen=True)
class RateRow:
    hs_code: str
    kind: str
    rate_pct: float
    spec: KindSpec


class DutyReferenceStore:
    """Read-only view over the ingested duty reference SQLite database."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._local = threading.local()
        meta = dict(self._conn().execute("SELECT key, value FROM meta"))
        self.as_of: str = meta.get("as_of", "")
        self.source: str = meta.get("source", "관세청 품목번호별 관세율표 (공공데이터포털)")

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
            self._local.conn = conn
        return conn

    def rates_for(self, hs_code: str) -> list[RateRow]:
        rows = self._conn().execute(
            "SELECT hs_code, kind, rate_pct FROM kr_rates WHERE hs_code = ? ORDER BY kind",
            (hs_code,),
        ).fetchall()
        return [RateRow(hs, kind, float(rate), classify_kind(kind)) for hs, kind, rate in rows]

    def foreign_rates(self, country_iso2: str, hs_prefix: str) -> list[dict[str, str]]:
        rows = self._conn().execute(
            "SELECT hs_code, name_en, name_ko, unit, rates_json FROM foreign_rates "
            "WHERE country_iso2 = ? AND hs_code LIKE ? ORDER BY hs_code LIMIT 200",
            (country_iso2, hs_prefix + "%"),
        ).fetchall()
        return [
            {"hs_code": r[0], "name_en": r[1], "name_ko": r[2], "unit": r[3], "rates_json": r[4]}
            for r in rows
        ]

    def agreement_hint_entries(self) -> dict[str, dict[str, object]]:
        """Same shape as the packaged snapshot's ``agreements`` mapping."""
        return {
            code: {
                "name_ko": name,
                "countries": list(countries),
                "certificate_form": form,
                "source_uri": _FTA_PORTAL,
            }
            for code, (name, countries, form) in AGREEMENTS.items()
        }


@lru_cache(maxsize=1)
def _store_for(path_text: str) -> DutyReferenceStore:
    return DutyReferenceStore(Path(path_text))


def load_store() -> DutyReferenceStore | None:
    """The active full-reference store, or None to keep snapshot behaviour.

    Reads the env var directly (not via ``Settings``) so pure view helpers
    stay usable in contexts where no full Backend profile is loaded.
    """
    import os

    path_text = os.environ.get("CUSTOMS_BACKEND_DUTY_REFERENCE_DB", "").strip()
    if not path_text:
        return None
    path = Path(path_text)
    if not path.is_file():
        return None
    return _store_for(str(path))


UNIPASS_SOURCE_URI = _UNIPASS
