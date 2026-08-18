"""
Lead Analysis Bot + Live Dashboard + Issue Analyzer
Runs 24/7 on Railway.
"""

import discord
import re
import os
import json
import threading
import unicodedata
from datetime import datetime
from collections import Counter
from flask import Flask, jsonify, send_from_directory

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

LEAD_CHANNELS = {
    1516479109457383424: "good-leads",
    1473373929564405782: "cold-leads",
    1489626024835813617: "hot-leads",
    1465742195649937601: "warm-leads",
}

REPORT_CHANNEL_ID = 1527723957355024454
ISSUES_CHANNEL_ID = 1489562320060551188
MOD_LOG_CHANNEL_ID = 1489562320060551188  # log moderation actions here (same as issues channel)

# ── Hate speech / slur detection ─────────────────────────────────────────────
# N-word: ALWAYS delete (any variant). "goy/goys/goyim": only when hateful context.
# Returns (should_delete, reason) tuple.

def _normalize_text(text):
    """Normalize unicode, strip zero-width chars, collapse separators, lowercase."""
    text = unicodedata.normalize("NFKD", text)
    # remove zero-width chars and soft-hyphens
    text = re.sub(r"[\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad]", "", text)
    # collapse: spaces, hyphens, underscores, dots, asterisks, commas between letters
    # e.g. "n i g g e r" -> "nigger", "n*gger" -> "ngger" (still matches partial)
    text = re.sub(r"[\s\-_./*+~,|]+", "", text)
    return text.lower()

def _detect_nword(text):
    """Detect N-word in any common bypass form. Returns True if found."""
    norm = _normalize_text(text)
    # After normalization, separators are stripped and letters lowered.
    # "nigger" -> "nigger", "n1gger" -> "n1gger", "n*gger" -> "ngger",
    # "n i g g e r" -> "nigger", "n|gger" -> "ngger", "knee-grow" -> "kneegrow"

    # Remove legitimate words containing n-gg patterns BEFORE matching
    exceptions = [
        "nigeria", "nigerian", "nigerians", "niger",
        "sniggered", "sniggering", "sniggers", "snigger",
        "niggling", "niggled", "niggles", "niggle",
        "niggardly", "niggard",
    ]
    for exc in exceptions:
        norm = norm.replace(exc, "x" * len(exc))

    patterns = [
        # Standard with vowel/leetspeak: nigger, n1gger, n!gger, nlgger
        r"n[i1l!|]+g+g?[3e@a]?r+",
        # Bypass: separator replaced the 'i': n*gger -> ngger, n|gger -> ngger
        r"ngg[3e@a]?r+",
        # -a ending with vowel: nigga, n1gga
        r"n[i1l!|]+g+[a@4]+h?",
        # -a ending no vowel: n*gga -> ngga
        r"ngg[a@4]+h?",
        # -ah ending
        r"n[i1l!|]+g+g?[a@4]+h+",
        # Phonetic: knee-grow -> kneegrow, kne-grow -> knegrow
        r"knee?g+row",
        # nibba (meme variant)
        r"n[i1l!|]+b{2,}[a@4]+",
    ]
    for p in patterns:
        if re.search(p, norm):
            return True
    return False

def _detect_goy(text):
    """Detect 'goy', 'goys', 'goyim' in any form — always delete."""
    norm = _normalize_text(text)
    # Catch: goy, goys, goyim, g0y, g0ys, g0yim, g0yz, etc.
    goy_pattern = r"g[0o]+y[sz]?|g[0o]+y[i1]+m"
    return bool(re.search(goy_pattern, norm))

def check_hate_speech(text):
    """Returns (should_delete: bool, reason: str or None)."""
    if _detect_nword(text):
        return True, "Racial slur (N-word) detected"
    if _detect_goy(text):
        return True, "Hate speech ('goy/goyim') detected"
    return False, None

AUTHOR_MAP = {
    "asersawy": "Abdelrahman", "vic_rattleheadv11": "Youssef",
    "davidsanchez0852": "David", "abdullah4561": "Abduallah",
    "zizo6018": "Zozz", "mariham0_0": "Zeinab",
    "omaraaakram": "Omar", "ann.brown": "Ann",
    "mohamedhawas0791": "Mohamed", "offerkings_manager_79793": "Manager",
    "mano069083": "Mano", "omardabatman": "Omar D",
}

# ── In-memory lead store ─────────────────────────────────────────────────────
LEADS_FILE = os.path.join(os.path.dirname(__file__), "leads.json")
leads_db = []
stats_lock = threading.Lock()
recently_processed = set()  # message IDs we already handled
MAX_PROCESSED = 500

def load_leads():
    """Load persisted leads from disk."""
    global leads_db
    try:
        if os.path.exists(LEADS_FILE):
            with open(LEADS_FILE, "r", encoding="utf-8") as f:
                leads_db = json.load(f)
            print(f"📂 Loaded {len(leads_db)} historical leads from disk")
    except Exception as e:
        print(f"⚠️ Could not load leads: {e}")

def save_leads():
    """Persist leads to disk."""
    try:
        with open(LEADS_FILE, "w", encoding="utf-8") as f:
            json.dump(leads_db, f, indent=2)
    except Exception as e:
        print(f"⚠️ Could not save leads: {e}")

# ── Flask app ────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

@app.route("/api/leads")
def api_leads():
    with stats_lock:
        return jsonify(leads_db[-200:][::-1])

@app.route("/api/weekly")
def api_weekly():
    """Return weekly report: last 7 days of leads aggregated."""
    with stats_lock:
        now = datetime.now()
        from datetime import timedelta
        cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        recent = [l for l in leads_db if l["time"][:10] >= cutoff]
        if not recent:
            return jsonify({"total": 0, "callers": [], "channels": [], "quality": {}, "avg_score": 0, "leads": []})

        total = len(recent)
        avg_score = round(sum(l["score"] for l in recent) / total, 1)

        by_caller = {}
        for l in recent:
            a = l["author"]
            if a not in by_caller:
                by_caller[a] = {"count": 0, "total_score": 0, "hot": 0, "high": 0, "medium": 0, "low": 0, "cold": 0, "missing_ask": 0}
            by_caller[a]["count"] += 1
            by_caller[a]["total_score"] += l["score"]
            by_caller[a][l["quality"].lower()] += 1
            if l.get("missing_ask"):
                by_caller[a]["missing_ask"] += 1

        callers = []
        for name, stats in sorted(by_caller.items(), key=lambda x: x[1]["count"], reverse=True):
            callers.append({
                "name": name,
                "count": stats["count"],
                "avg_score": round(stats["total_score"] / stats["count"], 1),
                "hot": stats["hot"], "high": stats["high"],
                "medium": stats["medium"], "low": stats["low"],
                "cold": stats.get("cold", 0),
                "missing_ask": stats["missing_ask"],
            })

        by_channel = {}
        for l in recent:
            ch = l["channel"]
            by_channel[ch] = by_channel.get(ch, 0) + 1
        channels = [{"name": k, "count": v} for k, v in sorted(by_channel.items(), key=lambda x: x[1], reverse=True)]

        quality = {"HOT": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "COLD": 0}
        for l in recent:
            quality[l["quality"]] += 1

        return jsonify({
            "total": total, "avg_score": avg_score,
            "callers": callers, "channels": channels,
            "quality": quality,
            "leads": recent[-50:][::-1],
        })

@app.route("/api/caller/<name>")
def api_caller(name):
    """Return individual caller performance."""
    with stats_lock:
        caller_leads = [l for l in leads_db if l["author"].lower() == name.lower()]
        if not caller_leads:
            return jsonify({"found": False, "name": name})

        total = len(caller_leads)
        avg_score = round(sum(l["score"] for l in caller_leads) / total, 1)

        quality = {"HOT": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "COLD": 0}
        by_channel = {}
        missing_ask = 0
        for l in caller_leads:
            quality[l["quality"]] += 1
            ch = l["channel"]
            by_channel[ch] = by_channel.get(ch, 0) + 1
            if l.get("missing_ask"):
                missing_ask += 1

        channels = [{"name": k, "count": v} for k, v in sorted(by_channel.items(), key=lambda x: x[1], reverse=True)]

        return jsonify({
            "found": True, "name": name, "total": total,
            "avg_score": avg_score, "quality": quality,
            "channels": channels, "missing_ask": missing_ask,
            "leads": caller_leads[-50:][::-1],
        })

@app.route("/api/stats")
def api_stats():
    """Return aggregate stats including leads per day."""
    with stats_lock:
        if not leads_db:
            return jsonify({"total": 0, "leads_per_day": [], "callers": [], "channels": []})
        from datetime import timedelta
        # Leads per day for last 14 days
        now = datetime.now()
        days = {}
        for i in range(14):
            d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            days[d] = 0
        for l in leads_db:
            d = l["time"][:10]
            if d in days:
                days[d] += 1
        lpd = [{"date": k, "count": v} for k, v in sorted(days.items())]

        # Caller summary
        by_caller = {}
        for l in leads_db:
            a = l["author"]
            if a not in by_caller:
                by_caller[a] = {"count": 0, "total_score": 0, "hot": 0, "high": 0, "medium": 0, "low": 0, "cold": 0}
            by_caller[a]["count"] += 1
            by_caller[a]["total_score"] += l["score"]
            by_caller[a][l["quality"].lower()] += 1
        callers = []
        for name, s in sorted(by_caller.items(), key=lambda x: x[1]["count"], reverse=True):
            callers.append({
                "name": name, "count": s["count"],
                "avg_score": round(s["total_score"] / s["count"], 1),
                "hot": s["hot"], "high": s["high"], "medium": s["medium"], "low": s["low"],
                "cold": s.get("cold", 0),
            })

        by_channel = {}
        for l in leads_db:
            ch = l["channel"]
            by_channel[ch] = by_channel.get(ch, 0) + 1
        channels = [{"name": k, "count": v} for k, v in sorted(by_channel.items(), key=lambda x: x[1], reverse=True)]

        total = len(leads_db)
        avg_score = round(sum(l["score"] for l in leads_db) / total, 1)
        high_q = sum(1 for l in leads_db if l["score"] >= 9)
        missing_ask = sum(1 for l in leads_db if l.get("missing_ask"))
        avg_per_day = round(total / max(len(lpd), 1), 1)

        return jsonify({
            "total": total, "avg_score": avg_score, "high_quality": high_q,
            "missing_ask": missing_ask, "avg_per_day": avg_per_day,
            "leads_per_day": lpd, "callers": callers, "channels": channels,
        })

@app.route("/health")
def health():
    return jsonify({"status": "ok", "leads": len(leads_db)})

# ── Lead Analyzer ────────────────────────────────────────────────────────────
def extract(pattern, text, group=1, flags=re.IGNORECASE):
    m = re.search(pattern, text, flags)
    if m:
        try: return m.group(group).strip()
        except: return None
    return None

def parse_money(text):
    """Parse money values from text — robustly extracts the first valid number.
    Handles: $420k, 200,000, ~220k, ~$80,000, $250,000 for both properties, etc."""
    if not text:
        return None
    # Strip parenthetical notes: "$260,000 (Based on...)" -> "$260,000"
    text = re.sub(r'\s*\(.*?\)\s*', '', text).strip()
    # Strip leading modifiers: ~, ≈, <, >, about, around, the least is, etc.
    text = re.sub(r'^[~≈<>]\s*', '', text)
    text = re.sub(r'^(?:about|around|roughly|the\s+least\s+is|lowest\s+is|bottom\s+line\s+is|firm\s+at|asking)\s*', '', text, flags=re.IGNORECASE)
    # Extract the first number pattern — ignores trailing text like "for both properties"
    m = re.search(r'\$?\s*([\d][\d,.]*[kKmM]?)', text)
    if not m:
        return None
    raw = m.group(1).strip().lower().replace('$', '').replace(',', '').replace(' ', '')
    if not raw:
        return None
    # Handle k suffix: 420k -> 420000, 220k -> 220000
    if raw.endswith('k'):
        raw_num = re.match(r'([\d.]+)', raw)
        if raw_num:
            try: return int(float(raw_num.group(1)) * 1000)
            except: pass
        try: return int(float(raw[:-1]) * 1000)
        except: pass
    if raw.endswith('m'):
        try: return int(float(raw[:-1]) * 1000000)
        except: pass
    try:
        val = int(float(raw))
        if 100 <= val < 1000:
            val *= 1000
        return val
    except:
        return None

def is_explicitly_missing(text):
    """Check if the value is explicitly stated as missing/none"""
    if not text:
        return True
    cl = text.lower().strip()
    missing_phrases = [
        'no number', 'none given', 'not stated', 'no specific',
        'no asking', 'open to offers', 'not mentioned', 'n/a',
        'no price', 'not provided', 'will consider', 'reasonable offer',
        'right offer', 'fair amount', 'has not looked', 'not in mind',
        'no in mind', 'did not mention', "didn't mention",
        'refused to provide', 'refused', 'wants an offer',
        'based on comps', 'not sure', 'unsure', 'no idea',
        "don't know", 'idk', 'no firm number', 'no set number',
        'no specific number', 'no exact number',
    ]
    return any(p in cl for p in missing_phrases)

def analyze_lead(content):
    if not content or len(content) < 20:
        return None
    # Strip markdown bold markers for field extraction
    clean = content.replace('**', '').replace('*', '').replace('__', '')

    name = extract(r"(?:Seller\s*(?:Full\s*)?Name|Full\s*Name|Contact\s*Name|Name)\s*[:]\s*(.+?)(?:\n|$)", clean)
    if not name:
        return None
    name = name.strip()

    phone = extract(r"(?:Sellers?\s*'?s?\s*Phone|Phone)\s*[:]\s*(.+?)(?:\n|$)", clean)
    address = extract(r"(?:Seller\s*Address|Address|Property\s*Address)\s*[:]\s*(.+?)(?:\n|$)", clean)
    email = extract(r"(?:Seller\s*'?s?\s*email|Email)\s*[:]\s*(.+?)(?:\n|$)", clean)
    redfin = extract(r"(https?://(?:www\.)?redfin\.com/\S+)", content)
    zillow = extract(r"(https?://(?:www\.)?zillow\.com/\S+)", content)

    # Market value - handle MV:, Market Value (MV):, Market Value:, sums like $X + $Y
    market = None
    for mv_pat in [
        r"Market\s*Value\s*\(MV\)\s*[:]\s*(.+?)(?:\n|$)",
        r"Market\s*Value\s*[:]\s*(.+?)(?:\n|$)",
        r"\bMV\s*[:]\s*(.+?)(?:\n|$)",
        r"(?:Redfin\s*Value|estimated\s*value)\s*[:]\s*(.+?)(?:\n|$)",
    ]:
        m_m = re.search(mv_pat, clean, re.IGNORECASE)
        if m_m:
            val_text = m_m.group(1).strip()
            # Handle sums: "$75,293 + $186,430" -> sum both
            if '+' in val_text:
                parts = val_text.split('+')
                total = 0
                found_any = False
                for part in parts:
                    pv = parse_money(part.strip())
                    if pv:
                        total += pv
                        found_any = True
                if found_any:
                    market = total
                    break
            else:
                # Handle "X on Redfin, Y on Zillow" — take first value
                pv = parse_money(val_text)
                if pv:
                    market = pv
                    break

    # Asking price - handle AP:, Asking Price (AP):, ~220k, "The least is $200,000", etc.
    ask = None
    m_a = None
    for ask_pat in [
        r"Asking\s*Price\s*\(AP\)\s*[:]\s*(.+?)(?:\n|$)",
        r"Asking\s*price\s*[:]\s*(.+?)(?:\n|$)",
        r"Asking\s*Price\s*[:]\s*(.+?)(?:\n|$)",
        r"\bAP\s*[:]\s*(.+?)(?:\n|$)",
        r"Target\s*Price\s*[:]\s*(.+?)(?:\n|$)",
        r"Asking\s*[:]\s*(.+?)(?:\n|$)",
    ]:
        m_a = re.search(ask_pat, clean, re.IGNORECASE)
        if m_a:
            ask_text = m_a.group(1).strip()
            if is_explicitly_missing(ask_text):
                ask = None  # Explicitly missing
                break
            ask = parse_money(ask_text)
            if ask:
                break
    if ask is None and m_a is None:
        # Try "bottom line" price
        m_bl = re.search(r"(?:bottom\s*line|lowest)\s*\$?([\d,.]+[kKmM]?)", clean, re.IGNORECASE)
        if m_bl:
            ask = parse_money(m_bl.group(1))

    cl = content.lower()
    high_mot = any(k in cl for k in [
        "divorce", "foreclosure", "probate", "vacant", "behind",
        "tired landlord", "distressed", "urgent", "asap", "needs to sell",
        "tired of renting", "no longer wants", "out of state", "out-of-state",
        "hassle", "tax burden", "tired of managing", "long distance",
        "elderly", "lost election", "refused to relocate",
        "no longer wants to manage", "eliminate the hassle",
        "wants to get rid of", "inherited", "relocating", "relocation",
        "health issues", "medical", "retiring", "retirement",
    ])
    fast_close = any(k in cl for k in [
        "asap", "immediately", "this week", "within 2 weeks", "30 days",
        "quick close", "fast", "ready to get done", "as soon as possible",
        "as soon as we can", "within 1 month", "within a month",
        "open to closing as soon as possible",
    ])
    # Use word-boundary matching to avoid substring false positives (e.g. "remodel" matching "remodeling")
    # Also skip negations: "no structural issues" is NOT poor condition
    poor_cond = False
    for k in [
        "needs repair", "needs work", "fixer", "mobile home", "trailer",
        "as-is", "bad condition", "renovation", "needs update",
        "nothing touched in", "dated", "remodel", "tear down", "teardown",
        "gut", "structural", "mold", "water damage", "fire damage",
        "hoarder", "condemned", "abandoned", "dilapidated",
    ]:
        m = re.search(r'\b' + re.escape(k) + r'\b', cl)
        if m:
            # Check for negation in the 40 chars before the match
            before = cl[max(0, m.start()-40):m.start()]
            if re.search(r'\b(no|not|without|zero|free of|clear of)\b', before):
                continue  # Negated — skip
            poor_cond = True
            break

    price_score = "medium"
    price_note = "no market data"
    if market and ask:
        ratio = ask / market
        if ratio <= 0.7: price_score = "high"; price_note = f"${ask:,} = {ratio*100:.0f}% of ${market:,} market"
        elif ratio >= 1.0: price_score = "low"; price_note = f"${ask:,} = {ratio*100:.0f}% of ${market:,} market"
        else: price_score = "medium"; price_note = f"${ask:,} = {ratio*100:.0f}% of ${market:,} market"
    elif market and ask is None:
        price_note = f"market ${market:,} but no asking price"
    elif ask and market is None:
        price_note = f"asking ${ask:,} but no market data"

    mot_score = 3 if high_mot else 2
    cond_score = 3 if poor_cond else 2
    close_score = 3 if fast_close else 2
    price_num = {"high": 3, "medium": 2, "low": 1}[price_score]
    total = mot_score + cond_score + close_score + price_num

    if total >= 10: quality = "HOT"
    elif total >= 9: quality = "HIGH"
    elif total >= 7: quality = "MEDIUM"
    else: quality = "LOW"

    return {
        "name": name, "phone": phone, "address": address, "email": email,
        "redfin": redfin, "zillow": zillow, "market": market, "ask": ask,
        "motivation": "high" if high_mot else "medium",
        "condition": "poor" if poor_cond else "medium",
        "closing": "fast" if fast_close else "medium",
        "price_score": price_score, "price_note": price_note,
        "total": total, "quality": quality, "missing_ask": ask is None and market is not None,
    }

def format_report(lead, author_name, channel_name):
    flags = []
    if lead["missing_ask"]: flags.append('**Missing asking price** -- ask: "What\'s the lowest you\'d accept?"')
    if lead["price_score"] == "low": flags.append("**Asking above market** -- seller may need reality check")
    if lead["motivation"] == "high": flags.append("**High motivation detected** -- prioritize this lead")
    if lead["closing"] == "fast": flags.append("**Fast close wanted** -- move quickly")
    flag_text = "\n".join(flags) if flags else "No red flags"

    quality_emoji = {"HOT": "🔥", "HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴", "COLD": "🧊"}[lead["quality"]]
    links = ""
    if lead["redfin"]: links += f"[Redfin]({lead['redfin']}) "
    if lead["zillow"]: links += f"[Zillow]({lead['zillow']})"
    if not links: links = "None"

    return (f"{quality_emoji} **Lead Analysis -- {lead['quality']}** ({lead['total']}/12)\n"
            f"**Seller:** {lead['name']}\n"
            f"**Address:** {lead['address'] or 'Not provided'}\n"
            f"**Phone:** {lead['phone'] or 'Not provided'}\n"
            f"**Email:** {lead['email'] or 'Not provided'}\n\n"
            f"**Scoring:**\n"
            f"- Motivation: **{lead['motivation']}** {'✅' if lead['motivation']=='high' else ''}\n"
            f"- Condition: **{lead['condition']}** {'🔧' if lead['condition']=='poor' else ''}\n"
            f"- Closing: **{lead['closing']}** {'⚡' if lead['closing']=='fast' else ''}\n"
            f"- Price: **{lead['price_score']}** -- {lead['price_note']}\n\n"
            f"**Links:** {links}\n\n"
            f"**Flags:**\n{flag_text}\n\n"
            f"*Posted by {author_name} in #{channel_name}*")

# ── Discord Bot ──────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
client = discord.Client(intents=intents)

async def backfill_leads():
    """Fetch historical leads from Discord channels and add to DB."""
    if leads_db:
        print(f"📊 Already have {len(leads_db)} leads, skipping backfill")
        return
    print("🔍 Backfilling historical leads from Discord...")
    total_added = 0
    for channel_id, channel_name in LEAD_CHANNELS.items():
        try:
            channel = client.get_channel(channel_id)
            if not channel:
                print(f"  ⚠️ Could not find channel {channel_name}")
                continue
            count = 0
            async for msg in channel.history(limit=200):
                if msg.author == client.user:
                    continue
                lead = analyze_lead(msg.content)
                if not lead:
                    continue
                with stats_lock:
                    # Check for duplicate
                    dup = any(
                        l["name"] == lead["name"] and l["time"] == msg.created_at.strftime("%Y-%m-%d %H:%M")
                        for l in leads_db
                    )
                    if dup:
                        continue
                    leads_db.append({
                        "author": msg.author.name, "name": lead["name"],
                        "score": lead["total"], "quality": lead["quality"],
                        "channel": channel_name,
                        "time": msg.created_at.strftime("%Y-%m-%d %H:%M"),
                        "address": lead.get("address"), "market": lead.get("market"),
                        "ask": lead.get("ask"), "missing_ask": lead["missing_ask"],
                    })
                    count += 1
            if count:
                save_leads()
                print(f"  ✅ {channel_name}: {count} leads")
                total_added += count
        except Exception as e:
            print(f"  ❌ {channel_name}: {e}")
    print(f"📊 Backfill complete: {total_added} historical leads loaded")

@client.event
async def on_message_edit(before, after):
    """Catch messages edited to include slurs after posting."""
    if after.author == client.user:
        return
    should_delete, reason = check_hate_speech(after.content)
    if should_delete:
        author = after.author.display_name or after.author.name
        channel_name = getattr(after.channel, 'name', 'DM')
        try:
            await after.delete()
            print(f"🚮 Deleted edited message from {author} in #{channel_name}: {reason}")
        except discord.Forbidden:
            print(f"❌ Missing 'Manage Messages' permission in #{channel_name}")
        except discord.NotFound:
            pass
        except Exception as e:
            print(f"❌ Delete failed: {e}")

@client.event
async def on_ready():
    print(f"✅ Lead Analysis Bot online as {client.user}")
    load_leads()
    # Backfill historical leads from Discord
    await backfill_leads()

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    # ── Auto-moderation: hate speech / slur detection ──
    should_delete, reason = check_hate_speech(message.content)
    if should_delete:
        author = message.author.display_name or message.author.name
        channel_name = getattr(message.channel, 'name', 'DM')
        try:
            await message.delete()
            print(f"🚮 Deleted message from {author} in #{channel_name}: {reason}")
        except discord.Forbidden:
            print(f"❌ Missing 'Manage Messages' permission in #{channel_name}")
        except discord.NotFound:
            pass  # already deleted
        except Exception as e:
            print(f"❌ Delete failed: {e}")
        return  # don't process further

    # Skip messages we already processed (prevents duplicate replies on reconnect)
    if message.id in recently_processed:
        return
    recently_processed.add(message.id)
    if len(recently_processed) > MAX_PROCESSED:
        # Keep only the most recent entries
        recently_processed.clear()

    # Issues channel — only respond to actual issues
    if message.channel.id == ISSUES_CHANNEL_ID:
        content = message.content.lower().strip()
        skip = ['fixed', 'back', 'nvm', 'yeah', 'ok', 'okay', 'joining', 'on it', 'checking', 'good', 'lol', 'lmao', 'haha', 'thanks', 'ty', 'thx', 'yes', 'no', 'yep', 'nope', 'right', 'same', 'done', 'wait', 'nvm', 'np', 'got it', 'sure', 'cool']
        if content in skip or len(content) < 8:
            return
        # Only respond to actual issues/problems
        issue_words = ['issue', 'problem', 'broken', 'not working', 'error', 'bug', 'crash', 'down', 'outage', 'no calls', 'mic', 'audio', 'phone', 'dialer', 'stuck', 'maintenance', 'lights out', 'phone not detected', 'ready mode', 'notification', 'alert', 'can\'t', 'cannot', 'wont', 'won\'t', 'doesnt', 'doesn\'t', 'isnt', 'isn\'t', 'missing', 'lost', 'failed', 'failing']
        if any(k in content for k in issue_words):
            try:
                await message.reply("Sounds like a skill issue but let me mention the team\n<@1140466002329612338> <@1241164795324010517>", mention_author=False)
            except:
                pass
        return

    # Lead channels
    if message.channel.id not in LEAD_CHANNELS:
        return

    channel_name = LEAD_CHANNELS[message.channel.id]
    author_name = AUTHOR_MAP.get(message.author.name, message.author.display_name)
    lead = analyze_lead(message.content)
    if not lead:
        return

    # Override quality for cold-leads channel — everything there is COLD
    if channel_name == "cold-leads":
        lead["quality"] = "COLD"
        lead["total"] = lead["total"]  # keep the raw score for reference

    with stats_lock:
        leads_db.append({
            "author": message.author.name, "name": lead["name"],
            "score": lead["total"], "quality": lead["quality"],
            "channel": channel_name, "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "address": lead.get("address"), "market": lead.get("market"),
            "ask": lead.get("ask"), "missing_ask": lead["missing_ask"],
        })
        save_leads()

    report = format_report(lead, author_name, channel_name)
    try:
        await message.reply(report, mention_author=False)
        print(f"📊 {author_name}: {lead['name']} ({lead['total']}/12) in #{channel_name}")
    except Exception as e:
        print(f"❌ Reply failed: {e}")

# ── Dashboard HTML ───────────────────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Lead Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0e17;--surface:#111827;--raised:#1a2235;--border:#1e293b;--bordersub:#162032;--text:#f1f5f9;--muted:#94a3b8;--dim:#64748b;--accent:#22d3ee;--accentdim:rgba(34,211,238,.12);--accentglow:rgba(34,211,238,.06);--success:#34d399;--successdim:rgba(52,211,153,.12);--warning:#fbbf24;--warningdim:rgba(251,191,36,.12);--danger:#f87171;--dangerdim:rgba(248,113,113,.12);--purple:#a78bfa;--purpledim:rgba(167,139,250,.12);--sans:DM Sans,system-ui,sans-serif;--mono:JetBrains Mono,monospace;--rad:8px;--radlg:12px}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-size:14px}
body{background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.5;-webkit-font-smoothing:antialiased;min-height:100vh}
.shell{display:grid;grid-template-columns:240px 1fr;min-height:100vh}
@media(max-width:900px){.shell{grid-template-columns:1fr}.sidebar{display:none}}
.sidebar{background:var(--surface);border-right:1px solid var(--border);padding:24px 16px;display:flex;flex-direction:column;gap:24px}
.sidebar-brand{display:flex;align-items:center;gap:10px;padding:0 4px}
.sidebar-brand .icon{width:32px;height:32px;background:var(--accentdim);border:1px solid rgba(34,211,238,.2);border-radius:8px;display:flex;align-items:center;justify-content:center}
.sidebar-brand .name{font-weight:700;font-size:1rem;letter-spacing:-.02em}
.sidebar-brand .name span{color:var(--accent)}
.sidebar-nav{display:flex;flex-direction:column;gap:2px}
.sidebar-nav a{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:6px;color:var(--muted);text-decoration:none;font-size:.88rem;font-weight:500;transition:all .15s;cursor:pointer}
.sidebar-nav a:hover{background:var(--raised);color:var(--text)}
.sidebar-nav a.active{background:var(--accentdim);color:var(--accent)}
.sidebar-nav a .dot{width:6px;height:6px;border-radius:50%;background:var(--dim)}
.sidebar-nav a.active .dot{background:var(--accent);box-shadow:0 0 6px var(--accent)}
.sidebar-footer{margin-top:auto;padding:12px;background:var(--raised);border-radius:var(--rad);font-size:.78rem;color:var(--dim);line-height:1.6}
.sidebar-footer .live-dot{display:inline-block;width:6px;height:6px;background:var(--success);border-radius:50%;margin-right:4px;animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.main{padding:28px 32px;overflow-x:hidden}
@media(max-width:600px){.main{padding:20px 16px}}
.main-header{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:28px}
.main-header h1{font-size:1.6rem;font-weight:700;letter-spacing:-.03em;line-height:1.2}
.main-header .meta{font-size:.78rem;color:var(--dim);font-family:var(--mono)}
.kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:28px}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:var(--radlg);padding:18px 20px;position:relative;overflow:hidden}
.kpi::after{content:"";position:absolute;top:0;left:0;right:0;height:2px}
.kpi.accent::after{background:var(--accent)}.kpi.success::after{background:var(--success)}.kpi.warning::after{background:var(--warning)}.kpi.purple::after{background:var(--purple)}
.kpi .label{font-size:.72rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin-bottom:6px}
.kpi .value{font-family:var(--mono);font-size:2rem;font-weight:700;line-height:1;letter-spacing:-.04em}
.kpi .value.accent{color:var(--accent)}.kpi .value.success{color:var(--success)}.kpi .value.warning{color:var(--warning)}.kpi .value.purple{color:var(--purple)}
.kpi .sub{font-size:.72rem;color:var(--dim);margin-top:4px;font-family:var(--mono)}
.section{margin-bottom:28px}
.section-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.section-head h2{font-size:.88rem;font-weight:600;color:var(--muted)}
.section-head .count{font-family:var(--mono);font-size:.72rem;color:var(--dim);background:var(--surface);padding:2px 8px;border-radius:4px;border:1px solid var(--border)}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:900px){.grid-2{grid-template-columns:1fr}}
.bar-list{background:var(--surface);border:1px solid var(--border);border-radius:var(--radlg);padding:16px}
.bar-item{display:grid;grid-template-columns:100px 1fr 48px;align-items:center;gap:12px;padding:7px 0}
.bar-item+.bar-item{border-top:1px solid var(--bordersub)}
.bar-item .name{font-size:.82rem;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-track{height:20px;background:var(--raised);border-radius:4px;overflow:hidden}
.bar-fill{height:100%;border-radius:4px;transition:width .6s cubic-bezier(.22,1,.36,1);min-width:2px}
.bar-fill.cyan{background:linear-gradient(90deg,rgba(34,211,238,.3),rgba(34,211,238,.15))}
.bar-fill.purple{background:linear-gradient(90deg,rgba(167,139,250,.3),rgba(167,139,250,.15))}
.bar-fill.green{background:linear-gradient(90deg,rgba(52,211,153,.3),rgba(52,211,153,.15))}
.bar-fill.amber{background:linear-gradient(90deg,rgba(251,191,36,.3),rgba(251,191,36,.15))}
.bar-item .count{font-family:var(--mono);font-size:.78rem;font-weight:600;color:var(--muted);text-align:right}
.score-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(56px,1fr));gap:6px}
.score-cell{background:var(--surface);border:1px solid var(--border);border-radius:var(--rad);padding:10px 6px;text-align:center;transition:border-color .15s}
.score-cell:hover{border-color:var(--accent)}
.score-cell .num{font-family:var(--mono);font-size:1.1rem;font-weight:700;line-height:1}
.score-cell .cnt{font-family:var(--mono);font-size:.68rem;color:var(--dim);margin-top:4px}
.score-cell.hot .num{color:var(--danger)}.score-cell.high .num{color:var(--success)}.score-cell.mid .num{color:var(--accent)}.score-cell.low .num{color:var(--warning)}
.table-wrap{background:var(--surface);border:1px solid var(--border);border-radius:var(--radlg);overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:.82rem}
thead th{background:var(--raised);padding:10px 14px;text-align:left;font-size:.68rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:1}
tbody td{padding:10px 14px;border-bottom:1px solid var(--bordersub);vertical-align:middle}
tbody tr{transition:background .1s}tbody tr:hover{background:var(--accentglow)}tbody tr:last-child td{border-bottom:none}
td.mono{font-family:var(--mono);font-size:.78rem}td.caller{font-weight:600;color:var(--purple)}
td.channel{font-size:.72rem;color:var(--dim);font-family:var(--mono)}
td.address{color:var(--muted);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:4px;font-family:var(--mono);font-size:.72rem;font-weight:600}
.badge.hot{background:var(--dangerdim);color:var(--danger)}.badge.high{background:var(--successdim);color:var(--success)}
.badge.mid{background:var(--accentdim);color:var(--accent)}.badge.low{background:var(--warningdim);color:var(--warning)}
.empty-state{text-align:center;padding:80px 20px;color:var(--dim)}
.empty-state p{font-size:.92rem;max-width:360px;margin:0 auto;line-height:1.7}
.loading-shimmer{background:linear-gradient(90deg,var(--surface) 25%,var(--raised) 50%,var(--surface) 75%);background-size:200% 100%;animation:shimmer 1.5s infinite;border-radius:var(--rad);height:48px;margin-bottom:12px}
@keyframes shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}
@media(prefers-reduced-motion:reduce){.loading-shimmer,.sidebar-footer .live-dot{animation:none}}
.page-footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;font-size:.72rem;color:var(--dim);font-family:var(--mono)}
.caller-select{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}
.caller-select button{background:var(--surface);border:1px solid var(--border);color:var(--muted);padding:6px 14px;border-radius:6px;font-family:var(--mono);font-size:.78rem;cursor:pointer;transition:all .15s}
.caller-select button:hover{background:var(--raised);color:var(--text)}
.caller-select button.active{background:var(--accentdim);border-color:var(--accent);color:var(--accent)}
.quality-bar{display:flex;height:8px;border-radius:4px;overflow:hidden;margin-top:8px}
.quality-bar .seg{height:100%}
.quality-bar .seg.hot{background:var(--danger)}.quality-bar .seg.high{background:var(--success)}
.quality-bar .seg.mid{background:var(--accent)}.quality-bar .seg.low{background:var(--warning)}
.view{display:none}.view.active{display:block}
</style>
</head>
<body>
<div class="shell">
<aside class="sidebar">
<div class="sidebar-brand"><div class="icon"><svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M8 1L14 5V11L8 15L2 11V5L8 1Z" stroke="currentColor" stroke-width="1.5" fill="none" style="color:var(--accent)"/><circle cx="8" cy="8" r="2" fill="var(--accent)"/></svg></div><div class="name">Lead<span>Monitor</span></div></div>
<nav class="sidebar-nav">
<a onclick="showView('dashboard')" id="nav-dashboard" class="active"><span class="dot"></span> Dashboard</a>
<a onclick="showView('weekly')" id="nav-weekly"><span class="dot"></span> Weekly Report</a>
<a onclick="showView('caller')" id="nav-caller"><span class="dot"></span> Caller Performance</a>
</nav>
<div class="sidebar-footer"><span class="live-dot"></span> Bot live on Railway<br>Watching 4 channels<br><span id="sidebar-count">0</span> leads tracked</div>
</aside>
<main class="main">
<div class="main-header"><div><h1 id="view-title">Lead Monitor</h1></div><div class="meta" id="last-updated">loading...</div></div>
<div id="view-dashboard" class="view active"><div id="app-dashboard"><div class="kpi-row"><div class="loading-shimmer"></div><div class="loading-shimmer"></div><div class="loading-shimmer"></div><div class="loading-shimmer"></div></div><div class="loading-shimmer" style="height:200px"></div></div></div>
<div id="view-weekly" class="view"><div id="app-weekly"><div class="loading-shimmer" style="height:300px"></div></div></div>
<div id="view-caller" class="view"><div id="app-caller"><div class="loading-shimmer" style="height:300px"></div></div></div>
<div class="page-footer"><span>leadmonitordiscordbot.com</span><span id="footer-time"></span></div>
</main>
</div>
<script>
let currentView = 'dashboard';
let callerList = [];
let selectedCaller = null;

function showView(v) {
  document.querySelectorAll('.view').forEach(function(e) { e.classList.remove('active'); });
  document.getElementById('view-' + v).classList.add('active');
  document.querySelectorAll('.sidebar-nav a').forEach(function(e) { e.classList.remove('active'); });
  document.getElementById('nav-' + v).classList.add('active');
  currentView = v;
  if (v === 'dashboard') { document.getElementById('view-title').textContent = 'Lead Monitor'; loadDashboard(); }
  else if (v === 'weekly') { document.getElementById('view-title').textContent = 'Weekly Report'; loadWeekly(); }
  else if (v === 'caller') { document.getElementById('view-title').textContent = 'Caller Performance'; loadCaller(); }
}

function scoreBadge(s) {
  if (s >= 10) return '<span class="badge hot">' + s + '/12</span>';
  if (s >= 9) return '<span class="badge high">' + s + '/12</span>';
  if (s >= 7) return '<span class="badge mid">' + s + '/12</span>';
  return '<span class="badge low">' + s + '/12</span>';
}

function barColor(i) { return ['cyan','purple','green','amber'][i % 4]; }

// Dashboard
async function loadDashboard() {
  try {
    var r = await fetch('/api/leads');
    if (!r.ok) throw new Error('API ' + r.status);
    var leads = await r.json();
    document.getElementById('last-updated').textContent = 'updated ' + new Date().toLocaleTimeString();
    document.getElementById('footer-time').textContent = 'auto-refresh 30s';
    document.getElementById('sidebar-count').textContent = leads.length;
    if (!leads.length) {
      document.getElementById('app-dashboard').innerHTML = '<div class="empty-state"><p>No leads recorded yet. The bot is watching all 4 channels and will populate this dashboard as leads come in.</p></div>';
      return;
    }
    var total = leads.length;
    var highQ = leads.filter(function(l) { return l.score >= 9; }).length;
    var avgScore = (leads.reduce(function(s,l) { return s + l.score; }, 0) / total).toFixed(1);
    var missingAsk = leads.filter(function(l) { return l.missing_ask; }).length;
    var byCaller = {};
    leads.forEach(function(l) { byCaller[l.author] = (byCaller[l.author] || 0) + 1; });
    var byChannel = {};
    leads.forEach(function(l) { byChannel[l.channel] = (byChannel[l.channel] || 0) + 1; });
    var scoreDist = {};
    leads.forEach(function(l) { scoreDist[l.score] = (scoreDist[l.score] || 0) + 1; });

    var h = '';
    h += '<div class="kpi-row">';
    h += '<div class="kpi accent"><div class="label">Total Leads</div><div class="value accent">' + total + '</div><div class="sub">all channels</div></div>';
    h += '<div class="kpi success"><div class="label">High Quality</div><div class="value success">' + highQ + '</div><div class="sub">' + Math.round(highQ/total*100) + '% of total</div></div>';
    h += '<div class="kpi purple"><div class="label">Avg Score</div><div class="value purple">' + avgScore + '</div><div class="sub">out of 12</div></div>';
    h += '<div class="kpi warning"><div class="label">Missing Ask</div><div class="value warning">' + missingAsk + '</div><div class="sub">' + Math.round(missingAsk/total*100) + '% gap</div></div></div>';

    h += '<div class="grid-2">';
    var cE = Object.entries(byCaller).sort(function(a,b) { return b[1] - a[1]; });
    var mC = cE[0][1];
    h += '<div class="section"><div class="section-head"><h2>Callers</h2><span class="count">' + cE.length + ' active</span></div><div class="bar-list">';
    cE.forEach(function(item, i) {
      var n = item[0], c = item[1];
      var p = (c/mC*100).toFixed(0);
      h += '<div class="bar-item"><div class="name">' + n + '</div><div class="bar-track"><div class="bar-fill ' + barColor(i) + '" style="width:' + p + '%"></div></div><div class="count">' + c + '</div></div>';
    });
    h += '</div></div>';

    var chE = Object.entries(byChannel).sort(function(a,b) { return b[1] - a[1]; });
    var mCh = chE[0][1];
    h += '<div class="section"><div class="section-head"><h2>Channels</h2><span class="count">' + chE.length + ' active</span></div><div class="bar-list">';
    chE.forEach(function(item, i) {
      var n = item[0], c = item[1];
      var p = (c/mCh*100).toFixed(0);
      h += '<div class="bar-item"><div class="name">' + n + '</div><div class="bar-track"><div class="bar-fill ' + barColor(i) + '" style="width:' + p + '%"></div></div><div class="count">' + c + '</div></div>';
    });
    h += '</div></div></div>';

    h += '<div class="section"><div class="section-head"><h2>Score Distribution</h2><span class="count">0-12 range</span></div><div class="score-grid">';
    for (var s = 12; s >= 4; s--) {
      var c = scoreDist[s] || 0;
      if (!c) continue;
      var cls = s >= 10 ? 'hot' : s >= 9 ? 'high' : s >= 7 ? 'mid' : 'low';
      h += '<div class="score-cell ' + cls + '"><div class="num">' + s + '</div><div class="cnt">' + c + '</div></div>';
    }
    h += '</div></div>';

    h += '<div class="section"><div class="section-head"><h2>Recent Leads</h2><span class="count">' + Math.min(leads.length, 50) + ' shown</span></div>';
    h += '<div class="table-wrap"><table><thead><tr><th>#</th><th>Caller</th><th>Seller</th><th>Address</th><th>Score</th><th>Channel</th><th>Time</th></tr></thead><tbody>';
    leads.slice(0, 50).forEach(function(l, i) {
      h += '<tr><td class="mono">' + (i+1) + '</td><td class="caller">' + l.author + '</td><td>' + l.name + '</td><td class="address">' + (l.address || '-') + '</td><td>' + scoreBadge(l.score) + '</td><td class="channel">' + l.channel + '</td><td class="mono">' + l.time + '</td></tr>';
    });
    h += '</tbody></table></div></div>';
    document.getElementById('app-dashboard').innerHTML = h;
  } catch(e) {
    document.getElementById('app-dashboard').innerHTML = '<div class="empty-state"><p>Could not reach API. Retrying in 30s.</p></div>';
  }
}

// Weekly Report
async function loadWeekly() {
  try {
    var r = await fetch('/api/weekly');
    if (!r.ok) throw new Error('API ' + r.status);
    var d = await r.json();
    document.getElementById('last-updated').textContent = 'updated ' + new Date().toLocaleTimeString();
    document.getElementById('footer-time').textContent = 'auto-refresh 30s';
    document.getElementById('sidebar-count').textContent = d.total;
    if (!d.total) {
      document.getElementById('app-weekly').innerHTML = '<div class="empty-state"><p>No leads this week yet.</p></div>';
      return;
    }
    var h = '';
    h += '<div class="kpi-row">';
    h += '<div class="kpi accent"><div class="label">Weekly Leads</div><div class="value accent">' + d.total + '</div><div class="sub">last 7 days</div></div>';
    h += '<div class="kpi success"><div class="label">Avg Score</div><div class="value success">' + d.avg_score + '</div><div class="sub">out of 12</div></div>';
    h += '<div class="kpi purple"><div class="label">HOT Leads</div><div class="value purple">' + d.quality.HOT + '</div><div class="sub">score 10+</div></div>';
    h += '<div class="kpi warning"><div class="label">HIGH Leads</div><div class="value warning">' + d.quality.HIGH + '</div><div class="sub">score 9</div></div>';
    h += '<div class="kpi" style="border-left:3px solid #4a9eff"><div class="label">COLD Leads</div><div class="value" style="color:#4a9eff">' + (d.quality.COLD||0) + '</div><div class="sub">cold channel</div></div></div>';

    h += '<div class="section"><div class="section-head"><h2>Caller Leaderboard</h2><span class="count">' + d.callers.length + ' active</span></div>';
    h += '<div class="table-wrap"><table><thead><tr><th>#</th><th>Caller</th><th>Leads</th><th>Avg Score</th><th>HOT</th><th>HIGH</th><th>MED</th><th>LOW</th><th>COLD</th><th>Missing Ask</th><th>Quality</th></tr></thead><tbody>';
    d.callers.forEach(function(c, i) {
      var total = c.hot + c.high + c.medium + c.low + (c.cold||0) || 1;
      h += '<tr><td class="mono">' + (i+1) + '</td><td class="caller">' + c.name + '</td><td class="mono">' + c.count + '</td><td class="mono">' + c.avg_score + '</td><td class="mono">' + c.hot + '</td><td class="mono">' + c.high + '</td><td class="mono">' + c.medium + '</td><td class="mono">' + c.low + '</td><td class="mono" style="color:#4a9eff">' + (c.cold||0) + '</td><td class="mono">' + c.missing_ask + '</td><td><div class="quality-bar"><div class="seg hot" style="width:' + (c.hot/total*100) + '%"></div><div class="seg high" style="width:' + (c.high/total*100) + '%"></div><div class="seg mid" style="width:' + (c.medium/total*100) + '%"></div><div class="seg low" style="width:' + (c.low/total*100) + '%"></div><div class="seg" style="width:' + ((c.cold||0)/total*100) + '%;background:#4a9eff"></div></div></td></tr>';
    });
    h += '</tbody></table></div></div>';

    h += '<div class="section"><div class="section-head"><h2>Recent Leads This Week</h2><span class="count">' + Math.min(d.leads.length, 50) + ' shown</span></div>';
    h += '<div class="table-wrap"><table><thead><tr><th>#</th><th>Caller</th><th>Seller</th><th>Address</th><th>Score</th><th>Channel</th><th>Time</th></tr></thead><tbody>';
    d.leads.slice(0, 50).forEach(function(l, i) {
      h += '<tr><td class="mono">' + (i+1) + '</td><td class="caller">' + l.author + '</td><td>' + l.name + '</td><td class="address">' + (l.address || '-') + '</td><td>' + scoreBadge(l.score) + '</td><td class="channel">' + l.channel + '</td><td class="mono">' + l.time + '</td></tr>';
    });
    h += '</tbody></table></div></div>';
    document.getElementById('app-weekly').innerHTML = h;
  } catch(e) {
    document.getElementById('app-weekly').innerHTML = '<div class="empty-state"><p>Could not reach API. Retrying in 30s.</p></div>';
  }
}

// Caller Performance
async function loadCaller() {
  try {
    if (!callerList.length) {
      var r = await fetch('/api/leads');
      if (!r.ok) throw new Error('API ' + r.status);
      var leads = await r.json();
      var seen = {};
      callerList = [];
      leads.forEach(function(l) {
        if (!seen[l.author]) { seen[l.author] = true; callerList.push(l.author); }
      });
    }
    var h = '<div class="caller-select">';
    callerList.forEach(function(n) {
      h += '<button onclick="selectCaller(\'' + n + '\')" id="btn-' + n + '" class="' + (selectedCaller === n ? 'active' : '') + '">' + n + '</button>';
    });
    h += '</div><div id="caller-detail">';
    if (!selectedCaller) {
      h += '<div class="empty-state"><p>Select a caller above to view their individual performance.</p></div>';
    } else {
      h += '<div class="loading-shimmer" style="height:200px"></div>';
    }
    h += '</div>';
    document.getElementById('app-caller').innerHTML = h;
    document.getElementById('last-updated').textContent = 'updated ' + new Date().toLocaleTimeString();
    document.getElementById('footer-time').textContent = 'auto-refresh 30s';
    if (selectedCaller) await loadCallerDetail(selectedCaller);
  } catch(e) {
    document.getElementById('app-caller').innerHTML = '<div class="empty-state"><p>Could not reach API. Retrying in 30s.</p></div>';
  }
}

async function selectCaller(name) {
  selectedCaller = name;
  document.querySelectorAll('.caller-select button').forEach(function(b) { b.classList.remove('active'); });
  var btn = document.getElementById('btn-' + name);
  if (btn) btn.classList.add('active');
  await loadCallerDetail(name);
}

async function loadCallerDetail(name) {
  try {
    var r = await fetch('/api/caller/' + encodeURIComponent(name));
    if (!r.ok) throw new Error('API ' + r.status);
    var d = await r.json();
    if (!d.found) {
      document.getElementById('caller-detail').innerHTML = '<div class="empty-state"><p>No data found for ' + name + '.</p></div>';
      return;
    }
    var total = d.quality.HOT + d.quality.HIGH + d.quality.MEDIUM + d.quality.LOW + (d.quality.COLD||0) || 1;
    var h = '';
    h += '<div class="kpi-row">';
    h += '<div class="kpi accent"><div class="label">Total Leads</div><div class="value accent">' + d.total + '</div><div class="sub">all time</div></div>';
    h += '<div class="kpi success"><div class="label">Avg Score</div><div class="value success">' + d.avg_score + '</div><div class="sub">out of 12</div></div>';
    h += '<div class="kpi purple"><div class="label">HOT Leads</div><div class="value purple">' + d.quality.HOT + '</div><div class="sub">' + (d.quality.HOT/total*100).toFixed(0) + '%</div></div>';
    h += '<div class="kpi warning"><div class="label">Missing Ask</div><div class="value warning">' + d.missing_ask + '</div><div class="sub">' + (d.missing_ask/d.total*100).toFixed(0) + '% gap</div></div></div>';

    h += '<div class="grid-2">';
    h += '<div class="section"><div class="section-head"><h2>Quality Breakdown</h2></div><div class="bar-list">';
    var qualities = [{l:'HOT',c:d.quality.HOT,cls:'hot'},{l:'HIGH',c:d.quality.HIGH,cls:'high'},{l:'MEDIUM',c:d.quality.MEDIUM,cls:'mid'},{l:'LOW',c:d.quality.LOW,cls:'low'},{l:'COLD',c:d.quality.COLD||0,cls:'low'}];
    qualities.forEach(function(q, i) {
      var p = (q.c/total*100).toFixed(0);
      h += '<div class="bar-item"><div class="name">' + q.l + '</div><div class="bar-track"><div class="bar-fill ' + barColor(i) + '" style="width:' + p + '%"></div></div><div class="count">' + q.c + '</div></div>';
    });
    h += '</div></div>';

    h += '<div class="section"><div class="section-head"><h2>Quality Distribution</h2></div>';
    h += '<div class="quality-bar" style="height:24px;border-radius:6px">';
    h += '<div class="seg hot" style="width:' + (d.quality.HOT/total*100) + '%"></div>';
    h += '<div class="seg high" style="width:' + (d.quality.HIGH/total*100) + '%"></div>';
    h += '<div class="seg mid" style="width:' + (d.quality.MEDIUM/total*100) + '%"></div>';
    h += '<div class="seg low" style="width:' + (d.quality.LOW/total*100) + '%"></div>';
    h += '<div class="seg" style="width:' + ((d.quality.COLD||0)/total*100) + '%;background:#4a9eff"></div>';
    h += '</div>';
    h += '<div style="display:flex;justify-content:space-between;font-size:.68rem;color:var(--dim);margin-top:6px;font-family:var(--mono)"><span>HOT ' + d.quality.HOT + '</span><span>HIGH ' + d.quality.HIGH + '</span><span>MED ' + d.quality.MEDIUM + '</span><span>LOW ' + d.quality.LOW + '</span><span style="color:#4a9eff">COLD ' + (d.quality.COLD||0) + '</span></div></div></div>';

    h += '<div class="section"><div class="section-head"><h2>Recent Leads</h2><span class="count">' + Math.min(d.leads.length, 50) + ' shown</span></div>';
    h += '<div class="table-wrap"><table><thead><tr><th>#</th><th>Seller</th><th>Address</th><th>Score</th><th>Channel</th><th>Time</th></tr></thead><tbody>';
    d.leads.slice(0, 50).forEach(function(l, i) {
      h += '<tr><td class="mono">' + (i+1) + '</td><td>' + l.name + '</td><td class="address">' + (l.address || '-') + '</td><td>' + scoreBadge(l.score) + '</td><td class="channel">' + l.channel + '</td><td class="mono">' + l.time + '</td></tr>';
    });
    h += '</tbody></table></div></div>';
    document.getElementById('caller-detail').innerHTML = h;
  } catch(e) {
    document.getElementById('caller-detail').innerHTML = '<div class="empty-state"><p>Could not load caller data.</p></div>';
  }
}

// Init
loadDashboard();
setInterval(function() {
  if (currentView === 'dashboard') loadDashboard();
  else if (currentView === 'weekly') loadWeekly();
  else if (currentView === 'caller') loadCaller();
}, 30000);
</script>
</body>
</html>
"""

@app.route("/")
def dashboard():
    return send_from_directory(os.path.dirname(__file__), "dashboard.html")

# ── Start ────────────────────────────────────────────────────────────────────
def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN not set!")
        exit(1)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("🌐 Dashboard on port 8080")
    print("Starting Lead Analysis Bot...")
    client.run(BOT_TOKEN)
