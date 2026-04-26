"""
GICS sector universes for the Tech Collector.

Each sector has a tuple of S&P 500 constituent tickers. Snapshot date is
noted per sector — these lists go stale as constituents change with
index rebalancing, mergers, and delistings.

Lists are alphabetized for easier maintenance. Coverage target is the
"main" members of each sector; edge cases (e.g. dual-class shares like
Alphabet A/C, News Corp A/B) typically include only the more-liquid class.

IMPORTANT: these lists are best-effort snapshots and are intended for
survivorship-biased backtesting research. For a production-quality
point-in-time universe you'd want a historical index-constituent dataset
from a vendor like S&P Dow Jones or WRDS.

Snapshot date for all sectors: 2026-04-19 (same as the original IT universe).
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Information Technology (72 symbols)
# This list is the one used from the start of the project — it's the
# verified set that matches the original tech_research_dataset.csv.
# ---------------------------------------------------------------------------
INFORMATION_TECHNOLOGY = (
    "AAPL", "ACN", "ADBE", "ADI", "ADP", "ADSK", "AKAM", "AMAT", "AMD",
    "ANET", "APH", "APP", "AVGO", "BR", "CDNS", "CDW", "CIEN", "COHR",
    "CRM", "CRWD", "CTSH", "DELL", "EPAM", "FFIV", "FICO", "FSLR", "FTNT",
    "GDDY", "GEN", "GLW", "HPE", "HPQ", "IBM", "INTC", "INTU", "IT",
    "JNPR", "KEYS", "KLAC", "LITE", "LRCX", "MCHP", "MPWR", "MSFT", "MSI",
    "MU", "NOW", "NTAP", "NVDA", "NXPI", "ON", "ORCL", "PANW", "PLTR",
    "PTC", "QCOM", "ROP", "SMCI", "SNDK", "SNPS", "STX", "SWKS", "TDY",
    "TEL", "TER", "TRMB", "TXN", "TYL", "VRSN", "VRT", "WDC", "ZBRA",
)

# ---------------------------------------------------------------------------
# Communication Services (~22 symbols)
# ---------------------------------------------------------------------------
COMMUNICATION_SERVICES = (
    "CHTR", "CMCSA", "DIS", "EA", "FOX", "FOXA", "GOOG", "GOOGL",
    "IPG", "LYV", "META", "NFLX", "NWS", "NWSA", "OMC", "PARA",
    "T", "TKO", "TMUS", "TTWO", "VZ", "WBD",
)

# ---------------------------------------------------------------------------
# Consumer Discretionary (~50 symbols)
# ---------------------------------------------------------------------------
CONSUMER_DISCRETIONARY = (
    "AMZN", "APTV", "AZO", "BBY", "BKNG", "BWA", "CCL", "CMG", "CZR",
    "DECK", "DHI", "DLTR", "DPZ", "DRI", "EBAY", "ETSY", "EXPE", "F",
    "GM", "GPC", "GRMN", "HAS", "HD", "HLT", "KMX", "LEN", "LKQ", "LOW",
    "LULU", "LVS", "MAR", "MCD", "MGM", "MHK", "NCLH", "NKE", "NVR",
    "ORLY", "PHM", "POOL", "RCL", "RL", "ROST", "SBUX", "TJX", "TPR",
    "TSCO", "TSLA", "ULTA", "WYNN", "YUM",
)

# ---------------------------------------------------------------------------
# Consumer Staples (~38 symbols)
# ---------------------------------------------------------------------------
CONSUMER_STAPLES = (
    "ADM", "BF.B", "BG", "CAG", "CHD", "CL", "CLX", "COST", "CPB",
    "DG", "EL", "GIS", "HRL", "HSY", "K", "KDP", "KHC", "KMB", "KO",
    "KR", "KVUE", "LW", "MDLZ", "MKC", "MNST", "MO", "PEP", "PG", "PM",
    "SJM", "STZ", "SYY", "TAP", "TGT", "TSN", "WBA", "WMT",
)

# ---------------------------------------------------------------------------
# Energy (~22 symbols)
# ---------------------------------------------------------------------------
ENERGY = (
    "APA", "BKR", "COP", "CTRA", "CVX", "DVN", "EOG", "EQT", "EXE",
    "FANG", "HAL", "HES", "KMI", "MPC", "OKE", "OXY", "PSX", "SLB",
    "TPL", "TRGP", "VLO", "WMB", "XOM",
)

# ---------------------------------------------------------------------------
# Financials (~72 symbols)
# ---------------------------------------------------------------------------
FINANCIALS = (
    "ACGL", "AFL", "AIG", "AIZ", "AJG", "ALL", "AMP", "AON", "AXP",
    "BAC", "BEN", "BK", "BLK", "BRK.B", "BRO", "BX", "C", "CB",
    "CBOE", "CFG", "CINF", "CME", "COF", "CPAY", "DFS", "ERIE", "FDS",
    "FI", "FIS", "FITB", "GL", "GPN", "GS", "HBAN", "HIG", "ICE",
    "JKHY", "JPM", "KEY", "KKR", "L", "MA", "MCO", "MET", "MKTX",
    "MMC", "MS", "MSCI", "MTB", "NDAQ", "NTRS", "PFG", "PGR", "PNC",
    "PRU", "PYPL", "RF", "RJF", "SCHW", "SPGI", "STT", "SYF", "TFC",
    "TRV", "TROW", "USB", "V", "WFC", "WRB", "WTW", "ZION",
)

# ---------------------------------------------------------------------------
# Health Care (~60 symbols)
# ---------------------------------------------------------------------------
HEALTH_CARE = (
    "A", "ABBV", "ABT", "AMGN", "BAX", "BDX", "BIIB", "BIO", "BMY",
    "BSX", "CAH", "CI", "CNC", "COO", "COR", "CRL", "CTLT", "CVS",
    "DGX", "DHR", "DVA", "DXCM", "ELV", "EW", "GEHC", "GILD", "HCA",
    "HOLX", "HSIC", "HUM", "IDXX", "INCY", "IQV", "ISRG", "JNJ", "LH",
    "LLY", "MCK", "MDT", "MOH", "MRK", "MRNA", "MTD", "OGN", "PFE",
    "PODD", "REGN", "RMD", "RVTY", "SOLV", "STE", "SYK", "TECH", "TFX",
    "TMO", "UHS", "UNH", "VRTX", "VTRS", "WAT", "ZBH", "ZTS",
)

# ---------------------------------------------------------------------------
# Industrials (~78 symbols)
# ---------------------------------------------------------------------------
INDUSTRIALS = (
    "AAL", "ADT", "ALK", "ALLE", "AME", "AOS", "AXON", "BA", "BLDR",
    "CARR", "CAT", "CHRW", "CMI", "CPRT", "CSX", "CTAS", "DAL", "DAY",
    "DE", "DOV", "EFX", "EMR", "ETN", "EXPD", "FAST", "FDX", "FTV",
    "GD", "GE", "GEV", "GNRC", "GWW", "HII", "HON", "HUBB", "HWM",
    "IEX", "IR", "ITW", "J", "JBHT", "JCI", "LDOS", "LHX", "LMT",
    "LUV", "MAS", "MMM", "NDSN", "NOC", "NSC", "ODFL", "OTIS", "PAYC",
    "PCAR", "PH", "PNR", "PWR", "ROK", "ROL", "RSG", "RTX", "SNA",
    "SWK", "TDG", "TT", "TXT", "UAL", "UBER", "UNP", "UPS", "URI",
    "VLTO", "VRSK", "WAB", "WM", "XYL",
)

# ---------------------------------------------------------------------------
# Materials (~28 symbols)
# ---------------------------------------------------------------------------
MATERIALS = (
    "ALB", "AMCR", "APD", "AVY", "BALL", "CE", "CF", "CTVA", "DD",
    "DOW", "ECL", "EMN", "FCX", "FMC", "IFF", "IP", "LIN", "LYB",
    "MLM", "MOS", "NEM", "NUE", "PKG", "PPG", "SHW", "STLD", "SW",
    "VMC",
)

# ---------------------------------------------------------------------------
# Real Estate (~32 symbols)
# ---------------------------------------------------------------------------
REAL_ESTATE = (
    "AMT", "ARE", "AVB", "BXP", "CBRE", "CCI", "CPT", "CSGP", "DLR",
    "DOC", "EQIX", "EQR", "ESS", "EXR", "FRT", "HST", "INVH", "IRM",
    "KIM", "MAA", "O", "PLD", "PSA", "REG", "SBAC", "SPG", "UDR",
    "VICI", "VTR", "WELL", "WY",
)

# ---------------------------------------------------------------------------
# Utilities (~31 symbols)
# ---------------------------------------------------------------------------
UTILITIES = (
    "AEE", "AEP", "AES", "ATO", "AWK", "CEG", "CMS", "CNP", "D",
    "DTE", "DUK", "ED", "EIX", "ES", "ETR", "EVRG", "EXC", "FE",
    "LNT", "NEE", "NI", "NRG", "PCG", "PEG", "PNW", "PPL", "SO",
    "SRE", "VST", "WEC", "XEL",
)


# ---------------------------------------------------------------------------
# Master lookup
# ---------------------------------------------------------------------------
SECTOR_UNIVERSES: dict[str, tuple[str, ...]] = {
    "Information Technology": INFORMATION_TECHNOLOGY,
    "Communication Services": COMMUNICATION_SERVICES,
    "Consumer Discretionary": CONSUMER_DISCRETIONARY,
    "Consumer Staples": CONSUMER_STAPLES,
    "Energy": ENERGY,
    "Financials": FINANCIALS,
    "Health Care": HEALTH_CARE,
    "Industrials": INDUSTRIALS,
    "Materials": MATERIALS,
    "Real Estate": REAL_ESTATE,
    "Utilities": UTILITIES,
}

# Snapshot date for all sector lists — update when you refresh
SECTOR_LISTS_SNAPSHOT_DATE = "2026-04-19"

# Warning notes for reference in manifests
SECTOR_LISTS_DATA_QUALITY_NOTE = (
    f"Sector symbol lists are hand-compiled snapshots dated "
    f"{SECTOR_LISTS_SNAPSHOT_DATE}. They reflect approximate S&P 500 "
    "sector membership as of that date and are survivorship-biased when "
    "applied to earlier periods. For production-grade backtesting, use "
    "a point-in-time historical-constituents dataset from S&P or a "
    "similar vendor."
)


def get_universe(sector: str) -> tuple[str, ...]:
    """Return the ticker tuple for the given sector name.

    Raises KeyError with a helpful message if the sector is unknown.
    """
    if sector not in SECTOR_UNIVERSES:
        known = sorted(SECTOR_UNIVERSES.keys())
        raise KeyError(
            f"Unknown sector '{sector}'. Known sectors: {known}"
        )
    return SECTOR_UNIVERSES[sector]


def list_sectors() -> list[str]:
    """Return the 11 GICS sector names in canonical order."""
    return list(SECTOR_UNIVERSES.keys())


def sector_slug(sector: str) -> str:
    """Turn a GICS sector name into a filename-safe slug.

    "Information Technology" -> "information-technology"
    "Consumer Discretionary" -> "consumer-discretionary"
    "Health Care"            -> "health-care"

    Used by the exporter for pack filenames. Lowercase + spaces to hyphens;
    no other punctuation appears in the current 11 GICS sector names so
    nothing else needs stripping. If future sector names acquire slashes
    or other oddities, extend this function rather than inlining the
    logic at the call site.
    """
    return sector.lower().replace(" ", "-")
