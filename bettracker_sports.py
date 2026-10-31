"""Deterministic, no-API sport inference for Discord wagers.

Explicit league / event labels beat entity and emoji clues. Ambiguous colleges
require a football/basketball cue; unknown sport is not guessed.
This file intentionally contains NO Discord/database dependencies.
"""
from __future__ import annotations

from functools import lru_cache
import re
from typing import Dict, List, Sequence, Set

# Tournament, broadcast, and league shorthand that identifies one sport.
# Avoid bare "Eastern Conference" or "Western Conference": NBA, WNBA, NHL
# and MLS all use them.
EXPLICIT_EVENTS: Dict[str, Sequence[str]] = {
    "NFL": (
        "NFL", "TNF", "MNF", "SNF", "THURSDAY NIGHT FOOTBALL",
        "MONDAY NIGHT FOOTBALL", "SUNDAY NIGHT FOOTBALL",
        "SUPER BOWL", "SUPERBOWL", "THURADAY NIGHT FOOTBALL",
        "THURS NIGHT FOOTBALL", "NFC CHAMPIONSHIP",
        "AFC CHAMPIONSHIP", "NFC EAST", "NFC WEST", "NFC NORTH",
        "NFC SOUTH", "AFC EAST", "AFC WEST", "AFC NORTH",
        "AFC SOUTH", "NFC/AFC", "AFC/NFC", "NFC TITLE GAME",
        "AFC TITLE GAME",
    ),
    "NCAAF": (
        "NCAAF", "CFB", "COLLEGE FOOTBALL", "COLLEGE FB",
        "COLLEGE FOOTBALL PLAYOFF", "CFP", "CFP NATIONAL CHAMPIONSHIP",
        "ROSE BOWL", "SUGAR BOWL", "COTTON BOWL", "ORANGE BOWL",
        "FIESTA BOWL", "PEACH BOWL",
    ),
    "NBA": (
        "NBA", "NBA CUP", "IN SEASON TOURNAMENT", "IN-SEASON TOURNAMENT",
        "INSEASON TOURNAMENT", "ECF", "WCF",
        "EASTERN CONFERENCE FINALS", "WESTERN CONFERENCE FINALS",
        "EASTERN CONFERENCE FINAL", "WESTERN CONFERENCE FINAL",
    ),
    "WNBA": ("WNBA", "COMMISSIONERS CUP", "COMMISSIONER'S CUP"),
    "NCAAB": (
        "NCAAB", "CBB", "COLLEGE BASKETBALL", "COLLEGE HOOPS",
        "MARCH MADNESS", "NCAA TOURNAMENT", "FINAL FOUR",
    ),
    "MLB": (
        "MLB", "BASEBALL", "WORLD SERIES", "ALCS", "NLCS",
        "ALDS", "NLDS", "AL WILD CARD", "NL WILD CARD",
    ),
    "NHL": ("NHL", "ICE HOCKEY", "HOCKEY", "STANLEY CUP"),
    "SOCCER": (
        "SOCCER", "EPL", "PREMIER LEAGUE", "UCL", "UEFA",
        "CHAMPIONS LEAGUE", "EUROPA LEAGUE", "CONFERENCE LEAGUE",
        "MLS", "LA LIGA", "LALIGA", "BUNDESLIGA", "SERIE A",
        "LIGUE 1", "LIGA MX", "FIFA", "FIFA WORLD CUP",
        "COPA AMERICA", "COPA LIBERTADORES", "FA CUP",
        "EFL CUP", "CARABAO CUP", "EL CLASICO", "CONCACAF",
        "EURO 2028", "EUROS", "AFC ASIAN CUP",
        "AFC CHAMPIONS LEAGUE",
    ),
    "TENNIS": (
        "TENNIS", "ATP", "WTA", "WIMBLEDON", "ROLAND GARROS",
        "AUSTRALIAN OPEN", "FRENCH OPEN", "DAVIS CUP", "LAVER CUP",
    ),
    "GOLF": ("GOLF", "PGA", "LPGA", "MASTERS TOURNAMENT", "RYDER CUP"),
    "ESPORTS": ("ESPORTS", "E-SPORTS", "CS2", "CSGO", "VALORANT", "DOTA"),
    "UFC": ("UFC",),
    "MMA": ("MMA",),
    "BOXING": ("BOXING",),
}

# Name aliases are conservative: an ambiguous nickname shared by two
# leagues is only resolved with a matching emoji or other unambiguous clue.
TEAM_ALIASES: Dict[str, Sequence[str]] = {
    "NFL": (
        "ARIZONA CARDINALS", "ATLANTA FALCONS", "BALTIMORE RAVENS",
        "BUFFALO BILLS", "CAROLINA PANTHERS", "CHICAGO BEARS",
        "CINCINNATI BENGALS", "CLEVELAND BROWNS", "DALLAS COWBOYS",
        "DENVER BRONCOS", "DETROIT LIONS", "GREEN BAY PACKERS",
        "HOUSTON TEXANS", "INDIANAPOLIS COLTS", "JACKSONVILLE JAGUARS",
        "KANSAS CITY CHIEFS", "LAS VEGAS RAIDERS", "LOS ANGELES CHARGERS",
        "LOS ANGELES RAMS", "MIAMI DOLPHINS", "MINNESOTA VIKINGS",
        "NEW ENGLAND PATRIOTS", "NEW ORLEANS SAINTS", "NEW YORK GIANTS",
        "NEW YORK JETS", "PHILADELPHIA EAGLES", "PITTSBURGH STEELERS",
        "SAN FRANCISCO 49ERS", "SEATTLE SEAHAWKS",
        "TAMPA BAY BUCCANEERS", "TENNESSEE TITANS",
        "WASHINGTON COMMANDERS", "49ERS", "NINERS", "FALCONS", "RAVENS",
        "BILLS", "BEARS", "BENGALS", "BROWNS", "COWBOYS", "BRONCOS",
        "LIONS", "PACKERS", "TEXANS", "COLTS", "JAGUARS", "JAGS",
        "CHIEFS", "RAIDERS", "CHARGERS", "RAMS", "DOLPHINS", "VIKINGS",
        "PATRIOTS", "PATS", "SAINTS", "EAGLES", "STEELERS",
        "SEAHAWKS", "BUCCANEERS", "BUCS", "TITANS", "COMMANDERS",
        "GIANTS", "JETS", "CARDINALS", "PANTHERS",
    ),
    "NBA": (
        "ATLANTA HAWKS", "BOSTON CELTICS", "BROOKLYN NETS",
        "CHARLOTTE HORNETS", "CHICAGO BULLS", "CLEVELAND CAVALIERS",
        "DALLAS MAVERICKS", "DENVER NUGGETS", "DETROIT PISTONS",
        "GOLDEN STATE WARRIORS", "HOUSTON ROCKETS", "INDIANA PACERS",
        "LOS ANGELES CLIPPERS", "LOS ANGELES LAKERS",
        "MEMPHIS GRIZZLIES", "MIAMI HEAT", "MILWAUKEE BUCKS",
        "MINNESOTA TIMBERWOLVES", "NEW ORLEANS PELICANS",
        "NEW YORK KNICKS", "OKLAHOMA CITY THUNDER", "ORLANDO MAGIC",
        "PHILADELPHIA 76ERS", "PHOENIX SUNS", "PORTLAND TRAIL BLAZERS",
        "SACRAMENTO KINGS", "SAN ANTONIO SPURS", "TORONTO RAPTORS",
        "UTAH JAZZ", "WASHINGTON WIZARDS", "HAWKS", "CELTICS", "NETS",
        "HORNETS", "BULLS", "CAVALIERS", "CAVS", "MAVERICKS", "MAVS",
        "NUGGETS", "PISTONS", "WARRIORS", "ROCKETS", "PACERS",
        "CLIPPERS", "LAKERS", "GRIZZLIES", "HEAT", "BUCKS",
        "TIMBERWOLVES", "PELICANS", "KNICKS", "THUNDER",
        "MAGIC", "SIXERS", "76ERS", "SUNS", "TRAIL BLAZERS",
        "BLAZERS", "SPURS", "RAPTORS", "JAZZ", "WIZARDS", "KINGS",
    ),
    "WNBA": (
        "ATLANTA DREAM", "CHICAGO SKY", "CONNECTICUT SUN",
        "DALLAS WINGS", "GOLDEN STATE VALKYRIES", "INDIANA FEVER",
        "LAS VEGAS ACES", "LOS ANGELES SPARKS", "MINNESOTA LYNX",
        "NEW YORK LIBERTY", "PHOENIX MERCURY", "SEATTLE STORM",
        "PORTLAND FIRE", "TORONTO TEMPO",
        "DREAM", "VALKYRIES", "FEVER", "ACES", "LYNX",
        "MYSTICS", "MERCURY", "CONNECTICUT SUN", "TORONTO TEMPO",
    ),
    "MLB": (
        "ARIZONA DIAMONDBACKS", "ATLANTA BRAVES", "BALTIMORE ORIOLES",
        "BOSTON RED SOX", "CHICAGO CUBS", "CHICAGO WHITE SOX",
        "CINCINNATI REDS", "CLEVELAND GUARDIANS", "COLORADO ROCKIES",
        "DETROIT TIGERS", "HOUSTON ASTROS", "KANSAS CITY ROYALS",
        "LOS ANGELES ANGELS", "LOS ANGELES DODGERS", "MIAMI MARLINS",
        "MILWAUKEE BREWERS", "MINNESOTA TWINS", "NEW YORK METS",
        "NEW YORK YANKEES", "PHILADELPHIA PHILLIES",
        "PITTSBURGH PIRATES", "SAN DIEGO PADRES",
        "SAN FRANCISCO GIANTS", "SEATTLE MARINERS",
        "ST LOUIS CARDINALS", "ST. LOUIS CARDINALS",
        "TAMPA BAY RAYS", "TEXAS RANGERS", "TORONTO BLUE JAYS",
        "WASHINGTON NATIONALS", "ATHLETICS", "DIAMONDBACKS",
        "D-BACKS", "BRAVES", "ORIOLES", "RED SOX", "CUBS", "WHITE SOX",
        "REDS", "GUARDIANS", "ROCKIES", "TIGERS", "ASTROS",
        "ROYALS", "ANGELS", "DODGERS", "MARLINS", "BREWERS",
        "TWINS", "METS", "YANKEES", "PHILLIES", "PIRATES",
        "CARDINALS", "GIANTS", "RANGERS",
        "PADRES", "MARINERS", "RAYS", "BLUE JAYS", "NATIONALS",
    ),
    "NHL": (
        "ANAHEIM DUCKS", "BOSTON BRUINS", "BUFFALO SABRES",
        "CALGARY FLAMES", "CAROLINA HURRICANES", "CHICAGO BLACKHAWKS",
        "COLORADO AVALANCHE", "COLUMBUS BLUE JACKETS", "DALLAS STARS",
        "DETROIT RED WINGS", "EDMONTON OILERS", "FLORIDA PANTHERS",
        "LOS ANGELES KINGS", "MINNESOTA WILD", "MONTREAL CANADIENS",
        "NASHVILLE PREDATORS", "NEW JERSEY DEVILS",
        "NEW YORK ISLANDERS", "NEW YORK RANGERS", "OTTAWA SENATORS",
        "PHILADELPHIA FLYERS", "PITTSBURGH PENGUINS",
        "SAN JOSE SHARKS", "SEATTLE KRAKEN", "ST LOUIS BLUES",
        "TAMPA BAY LIGHTNING", "TORONTO MAPLE LEAFS",
        "UTAH MAMMOTH", "VANCOUVER CANUCKS",
        "VEGAS GOLDEN KNIGHTS", "WASHINGTON CAPITALS",
        "WINNIPEG JETS", "DUCKS", "BRUINS", "SABRES", "FLAMES",
        "HURRICANES", "BLACKHAWKS", "AVALANCHE", "BLUE JACKETS",
        "RED WINGS", "OILERS", "CANADIENS", "PREDATORS",
        "DEVILS", "ISLANDERS", "SENATORS", "FLYERS", "PENGUINS",
        "SHARKS", "KRAKEN", "BLUES", "LIGHTNING", "MAPLE LEAFS",
        "MAMMOTH", "CANUCKS", "GOLDEN KNIGHTS", "CAPITALS",
        "PANTHERS", "KINGS", "JETS", "RANGERS",
    ),
    "SOCCER": (
        "MANCHESTER CITY", "MAN CITY", "MAN UTD", "MANCHESTER UNITED",
        "LIVERPOOL", "ARSENAL", "CHELSEA", "TOTTENHAM HOTSPUR",
        "NEWCASTLE UNITED", "WEST HAM", "ASTON VILLA", "BRIGHTON",
        "EVERTON", "NOTTINGHAM FOREST", "CRYSTAL PALACE", "BOURNEMOUTH",
        "BRENTFORD", "FULHAM", "SUNDERLAND", "BURNLEY",
        "LEEDS UNITED", "WOLVES", "REAL MADRID", "BARCELONA",
        "FC BARCELONA", "BAYERN MUNICH", "BORUSSIA DORTMUND",
        "DORTMUND", "PARIS SAINT GERMAIN", "PSG", "JUVENTUS",
        "INTER MILAN", "AC MILAN", "NAPOLI", "ATLETICO MADRID",
        "AJAX", "BENFICA", "SPORTING CP", "CELTIC FC", "LA GALAXY",
        "INTER MIAMI", "MIAMI FC", "SPURS", "AFC WIMBLEDON",
        "AFC BOURNEMOUTH",
    ),
}

# Player aliases are a deliberately limited, maintainable starter list, NOT
# a complete or automatically updated roster. Do not guess from unfamiliar names.
PLAYER_ALIASES: Dict[str, Sequence[str]] = {
    "NFL": (
        "PATRICK MAHOMES", "LAMAR JACKSON", "JOSH ALLEN", "JALEN HURTS",
        "BROCK PURDY", "ZAY FLOWERS", "SAQUON BARKLEY", "JUSTIN JEFFERSON",
        "JA'MARR CHASE", "JAMARR CHASE", "DRAKE MAYE", "TRAVIS KELCE",
        "BIJAN ROBINSON", "DERRICK HENRY", "KYREN WILLIAMS",
        "GEORGE KITTLE", "JAYDEN DANIELS", "CHRISTIAN MCCAFFREY",
    ),
    "NBA": (
        "LEBRON JAMES", "LUKA DONCIC", "STEPH CURRY", "STEPHEN CURRY",
        "GIANNIS ANTETOKOUNMPO", "NIKOLA JOKIC", "SHAl GILGEOUS",
        "SHAI GILGEOUS", "JAYSON TATUM", "SCOTTIE BARNES",
        "ANDREW NEMBHARD", "KARL ANTHONY TOWNS", "KARL-ANTHONY TOWNS",
        "JALEN BRUNSON", "KEVIN DURANT", "ANTHONY EDWARDS",
        "VICTOR WEMBANYAMA", "JA MORANT", "JOEL EMBIID",
        "TYRESE HALIBURTON", "DONOVAN MITCHELL", "BRANDON MILLER",
    ),
    "WNBA": (
        "CAITLIN CLARK", "AJA WILSON", "A'JA WILSON", "BREANNA STEWART",
        "ANGEL REESE", "SABRINA IONESCU", "PAIGE BUECKERS",
        "NAPHEESA COLLIER", "KELSEY PLUM",
    ),
    "MLB": (
        "SHOHEI OHTANI", "AARON JUDGE", "JUAN SOTO", "MOOKIE BETTS",
        "FREDDIE FREEMAN", "PAUL SKENES", "TARIK SKUBAL",
        "BOBBY WITT JR", "VLADIMIR GUERRERO JR", "ELLY DE LA CRUZ",
    ),
    "SOCCER": (
        "ERLING HAALAND", "HAALAND", "MOHAMED SALAH", "MO SALAH",
        "KYLIAN MBAPPE", "MBAPPE", "LIONEL MESSI", "MESSI",
        "CRISTIANO RONALDO", "VINICIUS JUNIOR", "VINICIUS JR",
        "BUKAYO SAKA", "JUDE BELLINGHAM", "HARRY KANE",
        "COLE PALMER", "PEDRI", "LAMINE YAMAL", "PHIL FODEN",
    ),
    "TENNIS": (
        "CARLOS ALCARAZ", "NOVAK DJOKOVIC", "JANNIK SINNER",
        "IGA SWIATEK", "COCO GAUFF", "ARYNA SABALENKA",
        "ALEXANDER ZVEREV", "TAYLOR FRITZ",
    ),
}

COLLEGE_NAMES: Sequence[str] = (
    "IOWA", "IOWA STATE", "LSU", "ALABAMA", "GEORGIA", "CLEMSON",
    "DUKE", "KANSAS", "KENTUCKY", "UCONN", "UNC", "NORTH CAROLINA",
    "FLORIDA", "FLORIDA STATE", "TENNESSEE", "AUBURN",
    "OHIO STATE", "MICHIGAN", "MICHIGAN STATE", "PENN STATE",
    "TEXAS", "TEXAS A&M", "TEXAS TECH", "OKLAHOMA", "OKLAHOMA STATE",
    "OREGON", "OREGON STATE", "USC", "UCLA", "GONZAGA",
    "ARIZONA", "ARIZONA STATE", "PURDUE", "ILLINOIS", "INDIANA",
    "WISCONSIN", "NEBRASKA", "NOTRE DAME", "BOISE STATE", "COLORADO",
    "UTAH", "BYU", "OLE MISS", "MISSISSIPPI STATE", "SOUTH CAROLINA",
    "MIAMI HURRICANES", "ST JOHNS", "ST. JOHN'S", "HOUSTON COUGARS",
    "BAYLOR", "VILLANOVA", "CREIGHTON", "MEMPHIS TIGERS",
)

FOOTBALL_CODES = {"NFL", "NCAAF"}
BASKETBALL_CODES = {"NBA", "WNBA", "NCAAB"}
EMOJI_CODES = {
    "🏈": "FOOTBALL",
    "🏀": "BASKETBALL",
    "⚾": "MLB",
    "🥎": "SOFTBALL",
    "🏒": "NHL",
    "⚽": "SOCCER",
    "🎾": "TENNIS",
    "⛳": "GOLF",
    "🥊": "BOXING",
}


@lru_cache(maxsize=4096)
def _alias_pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias).replace(r"\ ", r"\s+")
    return re.compile(r"(?<![A-Z0-9])" + escaped + r"(?![A-Z0-9])")


def _has(text: str, alias: str) -> bool:
    return _alias_pattern(alias).search(text) is not None


def _matches(text: str, rules: Dict[str, Sequence[str]]) -> List[str]:
    return [
        code for code, aliases in rules.items()
        if any(_has(text, alias) for alias in aliases)
    ]


def _entity_matches(text: str, emojis: Set[str]) -> List[str]:
    # Longest match wins within the same phrase: "New York Giants" should
    # not turn into ambiguous "Giants", while "Giants" alone is ambiguous.
    matched: Dict[str, Set[str]] = {}
    for sport, names in TEAM_ALIASES.items():
        for name in names:
            if _has(text, name):
                matched.setdefault(name, set()).add(sport)
    for sport, names in PLAYER_ALIASES.items():
        for name in names:
            if _has(text, name):
                matched.setdefault(name, set()).add(sport)

    # Do not double count a mascot that appears inside a full team name or
    # a short player name inside a longer player name.
    all_names = sorted(matched, key=len, reverse=True)
    usable = [
        name for name in all_names
        if not any(name != longer and _has(longer, name) for longer in all_names if len(longer) > len(name))
    ]
    unique: List[str] = []
    for name in usable:
        options = matched[name]
        if len(options) == 1:
            code = next(iter(options))
        else:
            # "Jets" can be NFL or NHL; "Kings" can be NBA or NHL.
            matched_emoji = {
                code for code in options
                if (
                    ("FOOTBALL" in emojis and code in FOOTBALL_CODES)
                    or ("BASKETBALL" in emojis and code in BASKETBALL_CODES)
                    or code in emojis
                )
            }
            if len(matched_emoji) != 1:
                continue
            code = next(iter(matched_emoji))
        if code not in unique:
            unique.append(code)

    # College school names are shared between football and basketball.
    # Only infer a college league when the emoji resolves it and no
    # specific pro/team/player clue already matched ("Texas Rangers" ≠ Texas).
    if not unique and any(_has(text, school) for school in COLLEGE_NAMES):
        if "FOOTBALL" in emojis:
            unique.append("NCAAF")
        elif "BASKETBALL" in emojis:
            unique.append("NCAAB")
    return unique


def infer_sports_from_text(text: str) -> List[str]:
    """Determine specific sports; return a generic label only when ambiguous.

    Multiple distinct codes indicate potential multi-sport DFS/parlay slips.
    Caller decides whether multiple codes mean MIXED or need review.
    """
    upper = str(text or "").upper()
    emojis = {code for icon, code in EMOJI_CODES.items() if icon in upper}
    explicit = _matches(upper, EXPLICIT_EVENTS)

    # Explicit sport/event labels are authoritative even if post emojis,
    # player names, or team names are contradictory decorations.
    if explicit:
        return explicit

    entities = _entity_matches(upper, emojis)
    if entities:
        return entities

    # Match football conference shorthand even without a sport label.
    # Soccer uses AFC too; known AFC club/competition names are caught
    # above as explicit soccer terms or team names.
    if _has(upper, "NFC") or _has(upper, "AFC"):
        return ["NFL"]

    if any(word in upper for word in ("NCAA", "COLLEGE")):
        if "FOOTBALL" in emojis:
            return ["NCAAF"]
        if "BASKETBALL" in emojis:
            return ["NCAAB"]

    found: List[str] = []
    for icon, sport in EMOJI_CODES.items():
        if icon not in upper:
            continue
        if sport not in found:
            found.append(sport)
    return found
