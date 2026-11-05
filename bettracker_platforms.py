"""Explicit post labels, not account syncing or verified placement. No migrations."""
import re

GROUPS = {
 'DFS_APP': 'Chalkboard|PrizePicks=prize picks|Underdog|Sleeper|Betr|Dabble=dabble us|Boom Sports=boom fantasy,boom|ParlayPlay=parlay play|DK Pick6=draftkings pick6,draftkings pick 6,pick6,pick 6|Smacktok=smack tok|Hotstreak=hot streak',
 'SPORTSBOOK': 'FanDuel=fan duel|Fanatics|DraftKings=draft kings|Caesars|theScore Bet=the score bet|Hard Rock=hardrock|BetMGM=bet mgm|bet365=bet 365|BetRivers=bet rivers|Bally Bet|ESPN BET',
 'PREDICTION': 'Novig|ProphetX=prophet x|Kalshi|Polymarket|OG|FD Predicts=fanduel predicts|DK Predictions=draftkings predictions',
 'SOCIAL': 'Fliff|Onyx=onyx odds|Thrillzz|Rebet|Courtside|Betr Social',
 'OFFSHORE': 'BetOnline=bet online|Bovada|MyBookie=my bookie|BetWhale=bet whale|BookMaker',
}
PLATFORMS = []
for kind, entries in GROUPS.items():
    for entry in entries.split('|'):
        name, _, extra = entry.partition('=')
        PLATFORMS.append((name, kind, [name.lower(), *filter(None, extra.split(','))]))

def canonical_platform(value):
    clean = ' '.join(str(value or '').lower().split())
    for name, kind, aliases in PLATFORMS:
        if clean in aliases:
            return name, kind
    return '', ''

def detect_platform(text):
    text = str(text or '')
    labels = re.findall(r'(?im)^\s*(?:platform|app|sportsbook)\s*:\s*([^\n|]+)', text)
    if labels:
        matches = {canonical_platform(label) for label in labels}
        return matches.pop() if len(matches) == 1 else ('', '')
    text = re.sub(r'https?://\S+|<[^>]+>', ' ', text)
    hits = []
    for name, kind, aliases in PLATFORMS:
        if name == 'OG':
            continue  # OG Anunoby is a player, not evidence of a platform.
        for alias in aliases:
            pattern = r'(?<![\w])' + re.escape(alias).replace(r'\ ', r'\s+') + r'(?![\w])'
            hits.extend((m.start(), m.end(), name, kind) for m in re.finditer(pattern, text, re.I))
    hits = [h for h in hits if not any(o[0] <= h[0] and o[1] >= h[1] and o[1]-o[0] > h[1]-h[0] for o in hits)]
    matches = {(h[2], h[3]) for h in hits}
    return matches.pop() if len(matches) == 1 else ('', '')

def recorded_platform(content, stored=''):
    return canonical_platform(stored)[0] or detect_platform(content)[0]

def is_moneyball(content):
    return bool(re.search(r'\bmoney[\s-]*ball\b', str(content or ''), re.I))

def moneyball_focus(content):
    """Return the explicitly marked Moneyball wager, never promotional explanation."""
    lines = str(content or '').splitlines()
    for line in lines:
        named = re.match(r'\s*moneyball\s+(?:player|leg|pick)\s*:\s*(.+)', line, re.I)
        if named:
            return named.group(1).strip()[:140] or 'Not explicitly labeled'

    for line in lines:
        if not ('🎯' in line or re.search(r'\(\s*moneyball\s*\)', line, re.I)):
            continue
        # Promo copy may begin with 🎯 and mention a player, but is not a bet leg.
        if re.search(r'(?i)\b(?:every made|counts as|moneyball\s*(?:promo)?\s*:|free pick|sign up)\b', line):
            continue
        if not re.search(
            r'(?i)\b(?:over|under)\s*\d|(?<!\w)[ou]\d+(?:\.\d+)?\b'
            r'|\b\d+(?:\.\d+)?\s*(?:pts?|points?|reb(?:ounds?)?|assists?|'
            r'ast|pra|sog|shots?|threes?|3s|yards?|receptions?)\b',
            line,
        ):
            continue
        clean = re.sub(r'<a?:\w+:\d+>|:[\w-]+:', ' ', line)
        clean = re.sub(r'\(\s*moneyball\s*\)', ' ', clean, flags=re.I)
        clean = clean.replace('🎯', ' ').replace('🍽️', ' ').replace('🍽', ' ')
        clean = re.sub(r'\s+', ' ', clean).strip(' |:,-')
        if clean:
            return clean[:140]
    return 'Not explicitly labeled'

def consume_platform(tokens):
    for length in range(min(4, len(tokens)), 0, -1):
        name, _ = canonical_platform(' '.join(tokens[:length]))
        if name:
            return name, tokens[length:]
    return '', tokens
