# engine/nbqsl.py
# Purpose: [ENGINE] nbQSL, instant QSL cards: the card (a ledger entry), its short link, the ids, topics, area and archive keys, the AIM line and the checking schedule.
# APIs: aim_line, archive_key, area_key, b36, box_interval, box_topic, card, card_link, clean, clean_design, clean_tag, country_for, decode, encode, entry_from_card, expired, is_call, nb_can_receive, norm_call, qso_id, rapid_until, reply_entry, rst_norm, utc_stamp, valid_entry, zip_check, zip_norm
# Uses: engine/branding.py
# Invariant: the link format (LINK_FIELDS), the entry's fields (ENTRY_TEXT), the ntfy topic names and the ledger's file names are shared with docs/qsl.html and the ledger collector; tests/test_nbqsl.py holds all three to these constants.
"""nbQSL -- instant QSL cards. Design owner: docs/NB_QSL.md (operator, 2026-11-22/23).

PURE: no network, no files, no clock unless passed in. The Client (engine/nbqsl_service.py) does the
talking; the collector (tools/nbqsl/collect.py) keeps the ledger; the card page (docs/qsl.html)
draws the card. All three read the formats from HERE.

THE CARD IS AN ENTRY; THE LINK ONLY POINTS AT IT (operator, 2026-11-23: "everything in link easily
risks being too big and getting cut off"). `card` builds the whole card -- name, address, grid, RSTs,
net, design -- as one ENTRY, which is posted to the recipient's ntfy box and the community inbox and
kept by the collector in the public ledger. The link (`encode`) carries only the id, the two calls,
the minute, the band and the mode: enough for the page to find the entry, and to draw a basic card
from the link alone when it cannot.

ONE nbQSL PER CONTACT. `qso_id` is a hash of the two calls, the minute, the band and the mode, so
the same contact always gets the same id: a second send is a duplicate, never a second card.

THE LEDGER lives on the releases repo's data branch (LEDGER_BRANCH), read straight from
raw.githubusercontent.com by the Client and by the card page -- release files cannot be read by a web
page (tested 2026-11-23: no CORS header). Two kinds of file, both plain JSON:
  * `<area>.json`            the last REPLY_WINDOW_H hours, by the RECIPIENT's `area_key` (the Client);
  * `archive/<month>/<area>.json`  everything, kept, by the QSO's month (`archive_key`) and the
                             recipient's area -- the page finds any card in one small file.
Clients never write to GitHub -- a key shipped in an install could wipe the releases repo; the
collector's key lives inside GitHub.
"""
import hashlib
import re
from urllib.parse import quote_plus, unquote_plus

#: The LINK's fields, in order. docs/qsl.html carries the same list (tests hold them equal).
LINK_FIELDS = ("v", "id", "from", "to", "utc", "band", "mode")
VERSION = "2"
#: What separates the link's fields. "_" BECAUSE IT SURVIVES AIM: the first live send used "~", which
#: is NetLogger's own record separator, so engine/aim_text.wire_safe turned every one into "-" on the
#: way out and the page could not read the link (operator, 2026-11-23). "_" is not in
#: aim_text.WIRE_UNSAFE, needs no escaping in a URL, and is in no call, band, mode or id -- a "_"
#: inside a value is escaped (%5F) anyway. tests/test_nbqsl.py sends a real AIM line through
#: wire_safe and holds the page to the same character.
LINK_SEP = "_"
#: The ENTRY (the card itself): its version, and every text field with its longest length. The card
#: page clips with the same table (tests hold them equal).
ENTRY_V = 2
ENTRY_TEXT = (("name", 30), ("city", 30), ("state", 24), ("county", 30), ("country", 30),
              ("grid", 6), ("band", 8), ("freq", 10), ("mode", 8), ("rst", 3), ("rst_r", 3),
              ("net", 40), ("note", 60),
              # THE RECIPIENT, AS THE SENDER LOGGED THEM (operator, 2026-11-23: fill the reply form
              # in). The card page pre-fills its reply form from these; the card never shows them.
              ("to_name", 30), ("to_city", 30), ("to_state", 24), ("to_county", 30),
              ("to_country", 30), ("to_grid", 6),
              # CLUB MEMBER NUMBERS (operator, 2026-11-23: "on clubs that have member numbers...
              # those should also be shown for both sender and recipient"): the sender's, the
              # recipient's, and the club's own label for them ("OM#"; "#" when it has none).
              ("num", 10), ("to_num", 10), ("numl", 6),
              # "1" when the opening check `zc` covers the signal report too (zip_check), so the
              # card page asks for both.
              ("rq", 1))
#: THE POSTAL-CODE CHECK (operator, 2026-11-23: "ask for a piece of data for validation... like their
#: postal code... a small friction but would hopefully discourage the casual"). A send carries `zc`,
#: `zip_check` of the recipient's postal code; a reply from the card page carries the same of what
#: was TYPED, or "" when the replier said theirs has changed. Never the code itself. FRICTION, NOT A
#: LOCK: a US ZIP has 100,000 values, so a script recovers one from `zc` in a moment -- the card id
#: mixed in only makes that per card. The card page computes the same (tests hold it equal).
ZC_LEN = 12

#: ntfy: the community inbox every card and reply is also posted to, and each call's own box.
NTFY_BASE = "https://ntfy.sh"
INBOX_TOPIC = "nbqsl_inbox_v2"
BOX_PREFIX = "nbqsl_box_"

#: WHICH NET BUDDY CAN TAKE A CARD IN ITS OWN BOX, by mesh surface letter (mesh/p2p_proto.SURFACE: c =
#: Companion, w / l / m = Client on Windows / Linux / Mac) -> the first version that answers one. The
#: Client since 3.6.38; Companion since 3.6.40, when the HUD learned to (hunter/hud_nbqsl). A phone, an
#: older build or an unknown version is sent the AIM link instead (operator, 2026-11-23: "if anyone
#: doesn't qualify (like their version is old) then it should default back to the link in AIM path").
#: Found live: two cards sat unread in the boxes of a 3.6.37 and a 3.6.39 Companion user.
NB_BOX_SINCE = {"w": "3.6.38", "l": "3.6.38", "m": "3.6.38", "c": "3.6.40"}

#: THE LEDGER: a branch of the PUBLIC releases repo, replaced hourly by the collector.
LEDGER_BRANCH = "nbqsl-data"
LEDGER_INDEX = "index.json"

#: How long a reply can be sent, and the ledger's live files keep it (operator, 2026-11-23: "72h is
#: easy so we can start there"). The archive keeps everything.
REPLY_WINDOW_H = 72
#: The ledger is checked at startup and this often; never faster -- it changes hourly.
LEDGER_EVERY_S = 2 * 3600
#: The own box, normally (ntfy keeps ~12 h, so a 2-hourly "since my last look" misses nothing).
BOX_EVERY_S = 2 * 3600
#: While a link reply is pending on a running net: listen, and check this often as the fallback
#: (operator, 2026-11-23: "every 5 or 10m maybe until end of net").
BOX_RAPID_S = 5 * 60
#: ...until this long after the net ends (operator: "maybe up to say 10m after the net").
RAPID_AFTER_NET_S = 10 * 60
#: Sent while not on a net: rapid for this long instead. PROPOSAL in docs/NB_QSL.md.
RAPID_OFF_NET_S = 2 * 3600

#: The card designs. The default is the operator's pick (2026-11-23: four to choose from).
LAYOUTS = ("classic", "postcard", "night", "map")
DEFAULT_DESIGN = "postcard"        # THE STANDARD CARD (operator, 2026-11-23: "I like the Postcard B one")
PALETTES = ("p1", "p2", "p3", "p4", "p5", "p6")
PATTERNS = ("g1", "g2", "g3", "g4")
TAG_MAX = 40

#: A country the call itself shows, so nobody types it: US and Canadian prefixes. The card page
#: carries the same two patterns (tests hold them equal).
US_CALL = r"^(A[A-L]|[KNW])[A-Z]{0,2}[0-9]"
CA_CALL = r"^(V[AEOY]|C[F-KYZ])[0-9]"

_CALL = re.compile(r"^[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,4}(/[A-Z0-9]{1,4})?$|^[A-Z0-9]{1,4}/[A-Z0-9]{3,8}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_ID = re.compile(r"^[a-f0-9]{10}$")
_UTC = re.compile(r"^\d{10}$")
#: THE NET CARDS (operator, 2026-11-23: "full graphic cool ones for at least a couple of the nets we
#: track... qsl's on that net get that net's branded QSL card"): club code (engine/nets.py `club`,
#: answered by engine/enrich.club_of_net) -> the design. The card page draws each with the club's own
#: logo, published beside it. A NET CARD WINS on its net, over the standard card and a supporter's own.
NET_CARDS = {"OMISS": "net-omiss", "3905CCN": "net-3905", "WASANET": "net-wasa", "HHHNET": "net-hhh"}
_DESIGN = re.compile(r"^((classic|postcard|night|map)(\.p[1-6](\.g[1-4])?)?|%s)$"
                     % "|".join(sorted(NET_CARDS.values())))
#: A short list for the supporter tagline and a reply's note: they sit beside a licensed callsign,
#: so this only has to catch the obvious. Whole words, any case.
_BLOCK = re.compile(r"\b(fuck|shit|cunt|bitch|nigger|faggot|asshole|bastard|dick|pussy|whore|slut)\w*\b", re.I)


def clean(text, limit):
    """One line of plain text: control characters gone, "~" gone (NetLogger's AIM separator), trimmed."""
    return _CTRL.sub(" ", str(text if text is not None else "")).replace("~", "-").strip()[:limit]


def nb_can_receive(ver, sf):
    """Can a Net Buddy advertising version `ver` on surface `sf` take a card in its own box? See
    NB_BOX_SINCE. Unknown surface or unparseable version -> False: the AIM link reaches anyone."""
    floor = NB_BOX_SINCE.get(str(sf or "").strip().lower()[:1])

    def key(v):
        m = re.match(r"\s*v?(\d+(?:\.\d+)*)", str(v or ""))
        return tuple(int(n) for n in m.group(1).split(".")) if m else ()
    a, b = key(ver), key(floor)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) >= b + (0,) * (n - len(b))


def is_call(call):
    return bool(_CALL.match(str(call or "").strip().upper()))


def norm_call(call):
    return str(call or "").strip().upper().rstrip("+")


def country_for(call):
    """The country a call shows on its face -- "USA", "Canada" -- or "" (then the operator types it)."""
    c = norm_call(call)
    if re.match(US_CALL, c):
        return "USA"
    if re.match(CA_CALL, c):
        return "Canada"
    return ""


def qso_id(from_call, to_call, utc, band, mode):
    """The contact's one id: the same contact always gets the same 10 hex characters, whichever
    side computes it (the calls are sorted)."""
    a, b = sorted((norm_call(from_call), norm_call(to_call)))
    key = "|".join((a, b, str(utc)[:10], str(band or "").strip().lower(), str(mode or "").strip().upper()))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def zip_norm(code):
    """A postal code as it is compared: capitals and digits only; a US ZIP+4 is its first five
    ("75644-1234" -> "75644"); a Canadian "V8J 2M2" -> "V8J2M2". "" for nothing usable."""
    c = re.sub(r"[^A-Z0-9]", "", str(code or "").upper())
    if c.isdigit() and len(c) == 9:
        c = c[:5]
    return c[:10]


def rst_norm(rst):
    """A signal report as it is compared: its digits, at most three ("5/9" -> "59"). "" for none."""
    return re.sub(r"[^0-9]", "", str(rst or ""))[:3]


def zip_check(card_id, code, rst=""):
    """The opening check for one card: ZC_LEN hex of sha256("nbqsl|<id>|<code>"), or of
    "nbqsl|<id>|<code>|<rst>" when there is a signal report too -- or "" when there is no code.
    docs/qsl.html computes the same.

    TWO PIECES, ONE CHECK (operator, 2026-11-23: "We could make it 2 pieces of information"; "Anyone
    clicking on the link would at minimum have to have listened to the QSO and remembered the signal
    reports"). `rst` is the report the RECIPIENT gave the sender, from the sender's log -- the one fact
    that is on no website. Hashed TOGETHER with the code, so neither can be worked out on its own."""
    z = zip_norm(code)
    if not z:
        return ""
    r = rst_norm(rst)
    text = "nbqsl|%s|%s" % (card_id, z) + ("|%s" % r if r else "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:ZC_LEN]


def utc_stamp(dt):
    """A datetime (UTC) -> "YYMMDDHHMM", the card's minute."""
    return dt.strftime("%y%m%d%H%M")


def b36(n):
    n = int(max(0, n))
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if not n:
            return out


def clean_design(design, perk=False):
    """The design a card may carry. Without the supporter perk, always the default; with it, a
    layout and optionally a palette and a pattern: "night.p3.g2"."""
    if not perk:
        return DEFAULT_DESIGN
    parts = str(design or "").split(".")
    layout = parts[0] if parts and parts[0] in LAYOUTS else DEFAULT_DESIGN
    out = [layout]
    if len(parts) > 1 and parts[1] in PALETTES:
        out.append(parts[1])
        if len(parts) > 2 and parts[2] in PATTERNS:
            out.append(parts[2])
    return ".".join(out)


def clean_tag(text, perk=False):
    """The supporter's tagline: "" without the perk or if the word filter catches it."""
    if not perk:
        return ""
    t = clean(text, TAG_MAX)
    return "" if _BLOCK.search(t) else t


def card(me, qso, sent_at, design=None, tag="", perk=False, via="nb", them=None, net_card=""):
    """THE CARD for one contact, as the entry that is posted and kept. `me`: {call, name, city, state,
    county, country, grid}. `qso`: {call, utc ("YYMMDDHHMM"), band, freq, mode, rst_sent, rst_rcvd,
    net}. `sent_at`: epoch seconds. `via`: "nb" (to a Net Buddy user) or "link" (posted on AIM).
    `me` and `them` may carry `num` (a club member number) and `me` its label `numl`.
    `them`: the recipient as logged, {name, city, state, county, country, grid, zip} -- the to_*
    fields and the postal-code check `zc`; the code itself is never kept. `net_card`: a NET_CARDS
    design, which wins (see NET_CARDS); anything else is ignored."""
    them = them or {}
    frm, to = norm_call(me.get("call")), norm_call(qso.get("call"))
    e = {"v": ENTRY_V, "k": "send", "from": frm, "to": to, "utc": str(qso.get("utc") or "")[:10],
         "name": me.get("name"), "city": me.get("city"), "state": me.get("state"),
         "county": me.get("county"), "country": me.get("country") or country_for(frm),
         "grid": me.get("grid"), "band": qso.get("band"), "freq": qso.get("freq"),
         "mode": qso.get("mode"), "rst": qso.get("rst_sent"), "rst_r": qso.get("rst_rcvd"),
         "net": qso.get("net"), "note": clean_tag(tag, perk), "design": net_card if net_card in NET_CARDS.values() else clean_design(design, perk),
         "via": "nb" if via == "nb" else "link", "s": int(sent_at)}
    for k in ("name", "city", "state", "county", "country", "grid", "num"):
        e["to_" + k] = them.get(k)
    e["num"], e["numl"] = me.get("num"), me.get("numl") if (me.get("num") or them.get("num")) else ""
    e["to_country"] = e["to_country"] or country_for(to)
    for k, n in ENTRY_TEXT:
        e[k] = clean(e.get(k), n)
    e["band"], e["mode"], e["grid"] = e["band"].lower(), e["mode"].upper(), e["grid"].upper()
    e["to_grid"] = e["to_grid"].upper()
    e["id"] = qso_id(frm, to, e["utc"], e["band"], e["mode"])
    # THE OPENING CHECK: the postal code, and on a LINK card the report they gave us as well
    # (zip_check). That report is then kept OFF the card -- the entry is public on ntfy, and carrying
    # it would hand the answer to whoever opens the link.
    rpt = rst_norm(e["rst_r"]) if e["via"] == "link" else ""
    e["zc"] = zip_check(e["id"], them.get("zip"), rpt)
    e["rq"] = "1" if (e["zc"] and rpt) else ""
    if e["via"] == "link":
        e["rst_r"] = ""
    return e


def entry_from_card(c, kind="send", via="nb", now=None):
    """The same card as a `kind` entry ("send" or "reply") sent `via` at `now`."""
    return dict(c, k=kind, via="nb" if via == "nb" else "link", s=int(now if now is not None else c.get("s") or 0))


def reply_entry(offer, me, now, design=None, tag="", perk=False, rst_sent=""):
    """Our card back for an OFFER we received: the same contact the other way round, our sender block,
    their RST for us carried over, and `rst_sent` -- the report WE gave, from our matched log row
    (a card back used to go out with its RST blank). `me` as for `card`."""
    e = card(me, {"call": offer["from"], "utc": offer["utc"], "band": offer["band"], "mode": offer["mode"],
                  "rst_sent": rst_sent, "rst_rcvd": offer.get("rst", ""), "net": offer.get("net", "")},
             now, design=design, tag=tag, perk=perk, via="nb",
             # A NET CARD IS ANSWERED ON THE SAME NET'S CARD -- the card page's reply does the same.
             net_card=offer.get("design") if offer.get("design") in NET_CARDS.values() else "")
    e["k"], e["id"] = "reply", offer["id"]
    e["to_num"] = clean(offer.get("num"), 10)
    e["numl"] = e["numl"] or clean(offer.get("numl"), 6)
    return e


def encode(c):
    """The link fragment for a card: LINK_FIELDS in order, LINK_SEP-separated, each URL-encoded. About
    45 characters -- the rest of the card is looked up."""
    vals = dict(c, v=VERSION)
    return LINK_SEP.join(quote_plus(clean(vals.get(k, ""), 12), safe="").replace("_", "%5F")
                         for k in LINK_FIELDS)


def decode(fragment):
    """The link's fields back, or None when it is not a valid version-2 nbQSL link. The page applies
    the same checks (docs/qsl.html `parse`)."""
    parts = str(fragment or "").lstrip("#").split(LINK_SEP)
    if len(parts) != len(LINK_FIELDS) or parts[0] != VERSION:
        return None
    c = {k: clean(unquote_plus(parts[i]), 12) for i, k in enumerate(LINK_FIELDS)}
    c["from"], c["to"] = c["from"].upper(), c["to"].upper()
    c["band"], c["mode"] = c["band"].lower(), c["mode"].upper()
    if not _ID.match(c["id"]) or not is_call(c["from"]) or not is_call(c["to"]) or not _UTC.match(c["utc"]):
        return None
    return c


def card_link(c):
    from engine.branding import QSL_PAGE_URL
    return QSL_PAGE_URL + "#" + encode(c)


def aim_line(to_call, link):
    """THE AIM LINE (operator, 2026-11-23): their call first, so their own-call alert catches it."""
    return ("[b]%s - This is an instant nbQSL card.  Please open the link to view and reply![/b] - %s"
            % (norm_call(to_call), str(link or "").strip()))


def box_topic(call):
    """A call's own nbQSL box on ntfy. docs/qsl.html builds the same name."""
    return BOX_PREFIX + re.sub(r"[^a-z0-9]", "_", norm_call(call).lower())


def area_key(call):
    """The ledger file a call's QSLs live in: the first digit in the call, then its first letter if
    A, K, N or W, else X. W6XYZ -> "6W", VE3ABC -> "3X", a call with no digit -> "XX"
    (operator, 2026-11-23: split "per call district or more (like [including] a/k/n as their own files)")."""
    c = norm_call(call)
    m = re.search(r"\d", c)
    digit = m.group(0) if m else "X"
    first = c[:1] if c[:1] in ("A", "K", "N", "W") else "X"
    return digit + first


def archive_key(utc):
    """The archive folder an entry is kept in: the QSO's month, "2026-11" -- which the link carries, so
    the card page knows the one file to read."""
    u = str(utc or "")
    return "20%s-%s" % (u[0:2], u[2:4])


def valid_entry(e):
    """An entry made safe, or None. Used on everything read from ntfy or the ledger -- anyone can post
    to a public topic, so nothing is trusted until it passes this."""
    if not isinstance(e, dict) or e.get("v") != ENTRY_V or e.get("k") not in ("send", "reply"):
        return None
    out = {"v": ENTRY_V, "k": e["k"], "via": "nb" if e.get("via") == "nb" else "link"}
    out["id"] = clean(e.get("id"), 10).lower()
    out["from"], out["to"] = norm_call(e.get("from")), norm_call(e.get("to"))
    out["utc"] = clean(e.get("utc"), 10)
    if not _ID.match(out["id"]) or not is_call(out["from"]) or not is_call(out["to"]) \
            or not _UTC.match(out["utc"]) or out["from"] == out["to"]:
        return None
    for k, n in ENTRY_TEXT:
        out[k] = clean(e.get(k), n)
    out["band"], out["mode"], out["grid"] = out["band"].lower(), out["mode"].upper(), out["grid"].upper()
    out["to_grid"] = out["to_grid"].upper()
    zc = clean(e.get("zc"), ZC_LEN).lower()
    out["zc"] = zc if re.fullmatch(r"[a-f0-9]{%d}" % ZC_LEN, zc) else ""
    out["rq"] = "1" if (out["zc"] and out["rq"] == "1") else ""
    if _BLOCK.search(out["note"]):
        out["note"] = ""
    d = clean(e.get("design"), 20)
    out["design"] = d if _DESIGN.match(d) else DEFAULT_DESIGN
    try:
        out["s"] = int(e.get("s") or 0)
    except (TypeError, ValueError):
        out["s"] = 0
    return out


def rapid_until(sent_at, on_net, net_ended_at=None):
    """Until when the own box gets rapid attention for one link nbQSL, or None for "while the net
    runs". On a net: until RAPID_AFTER_NET_S after it ends. Off a net: RAPID_OFF_NET_S after sending."""
    if not on_net:
        return sent_at + RAPID_OFF_NET_S
    return None if net_ended_at is None else net_ended_at + RAPID_AFTER_NET_S


def box_interval(waiting, now, net_running, net_ended_at=None):
    """How often to look at the own box now. `waiting`: the link nbQSLs still waiting for a reply,
    each {sent_at, on_net}. Rapid only while one of them is inside its rapid window; otherwise the
    normal BOX_EVERY_S. One look covers every outstanding nbQSL, so 30 sends cost no more than one."""
    for w in waiting or ():
        if now >= w.get("sent_at", 0) + REPLY_WINDOW_H * 3600:
            continue
        until = rapid_until(w.get("sent_at", 0), w.get("on_net", False),
                            None if net_running else net_ended_at)
        if until is None or now < until:
            return BOX_RAPID_S
    return BOX_EVERY_S


def expired(sent_at, now):
    """An unanswered nbQSL past the window is "no reply" and no longer waited for."""
    return now >= sent_at + REPLY_WINDOW_H * 3600
