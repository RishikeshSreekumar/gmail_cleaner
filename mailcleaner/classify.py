"""Deterministic classifier. No LLM, no network, no email bodies.

Three independent dimensions are produced for every message:
  category/subcategory   what kind of mail it is
  attention              how much it wants from you now
  retention              what should happen to it
plus a `protected` flag that bulk cleanup refuses to touch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Attention states, most urgent first.
ACTION_REQUIRED = "action required"
POTENTIALLY_IMPORTANT = "potentially important"
READ_LATER = "read later"
INFORMATIONAL = "informational"
NO_ATTENTION = "no attention"

# Retention states.
PROTECTED = "protected"
KEEP = "keep"
ARCHIVE = "archive"
REVIEW = "review"
CLEANUP = "cleanup candidate"

AUTOMATED_LOCALPARTS = (
    "noreply", "no-reply", "donotreply", "do-not-reply", "notification",
    "notifications", "mailer", "mailer-daemon", "updates", "news", "info",
    "alerts", "alert", "bounce", "support", "hello", "team", "automated",
    "postmaster", "newsletter", "digest", "marketing", "email", "mail",
)

def _kw(*words: str) -> re.Pattern:
    return re.compile(r"\b(" + "|".join(words) + r")\b", re.I)


#: Keyword packs, one per language or country. Each family's pattern is the
#: union of the enabled packs; an account with no `locales` set gets all of
#: them. English and India together reproduce the original single list.
LOCALES: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "finance": (
            "invoice", "invoices", "statement", "statements", "receipt", "receipts",
            "transaction", "payment", "paid", "credited", "debited", "refund", "bill",
            "billing", "tax", "salary", "payslip", "premium", "portfolio",
            "dividend", "settlement",
        ),
        "security": (
            "otp", "one-time", "verification", "verify", "sign-in", "sign in",
            "signin", "login", "log in", "password", "2fa", "two-factor",
            "security alert", "suspicious", "unauthorized", "authenticate",
            "passcode", "recovery",
        ),
        "orders": (
            "order", "shipped", "shipping", "delivered", "delivery", "dispatched",
            "out for delivery", "return", "refund initiated", "tracking", "courier",
        ),
        "travel": (
            "boarding", "booking", "reservation", "ticket", "itinerary",
            "check-in", "flight", "hotel", "e-ticket", "confirmed trip",
        ),
        "expiry": (
            "expire", "expires", "expiring", "expired", "renewal", "renew", "due",
            "overdue", "last day", "deadline", "action required", "final notice",
            "suspended", "will be deleted",
        ),
        "promo": (
            "sale", "off", "discount", "deal", "deals", "offer", "offers", "coupon",
            "save", "free", "limited time", "exclusive", "flat", "cashback",
            "upgrade now",
        ),
        "newsletter": (
            "newsletter", "digest", "weekly", "daily", "roundup", "issue", "edition",
            "this week", "top stories", "read more",
        ),
        "finance_domains": (
            "bank", "paypal", "stripe", "insurance", "amex", "visa", "mastercard",
        ),
    },
    "in": {
        "finance": ("gst", "tds", "emi", "mutual fund"),
        "travel": ("pnr",),
        "finance_domains": (
            "icici", "hdfc", "sbi", "axis", "kotak", "razorpay", "zerodha", "groww",
            "upstox", "acko", "policybazaar", "incometax", "gov.in", "cred.club",
        ),
    },
    "us": {
        "finance": ("w-2", "1099", "401k", "ach", "escrow"),
        "finance_domains": (
            "irs.gov", "chase", "wellsfargo", "bankofamerica", "capitalone",
            "venmo", "schwab", "fidelity", "vanguard", "discover",
        ),
    },
    "uk": {
        "finance": ("hmrc", "direct debit", "council tax", "vat", "payslip"),
        "finance_domains": (
            "hmrc.gov.uk", "barclays", "hsbc", "lloyds", "natwest", "monzo",
            "revolut", "nationwide", "santander",
        ),
    },
    "de": {
        "finance": (
            "rechnung", "zahlung", "kontoauszug", "quittung", "überweisung",
            "gutschrift", "steuer", "lastschrift", "beitrag",
        ),
        "security": (
            "passwort", "anmeldung", "bestätigungscode", "sicherheitswarnung",
            "verifizierung", "einmalcode",
        ),
        "orders": (
            "bestellung", "versand", "versandt", "lieferung", "zugestellt",
            "rücksendung", "sendungsverfolgung",
        ),
        "travel": ("buchung", "reservierung", "flug", "bordkarte"),
        "expiry": ("läuft ab", "abgelaufen", "frist", "fällig", "mahnung",
                   "letzte erinnerung"),
        "promo": ("rabatt", "angebot", "gutschein", "sparen", "kostenlos"),
        "newsletter": ("wöchentlich", "ausgabe"),
        "finance_domains": ("sparkasse", "volksbank", "commerzbank", "deutsche-bank",
                            "n26", "elster"),
    },
    "fr": {
        "finance": ("facture", "paiement", "relevé", "reçu", "virement",
                    "remboursement", "impôts", "prélèvement"),
        "security": ("mot de passe", "connexion", "code de vérification",
                     "alerte de sécurité"),
        "orders": ("commande", "expédié", "expédition", "livraison", "livré", "suivi"),
        "travel": ("réservation", "votre vol", "billet", "carte d'embarquement"),
        "expiry": ("expiration", "échéance", "renouvellement", "dernier rappel"),
        "promo": ("réduction", "offre", "soldes", "code promo", "gratuit"),
        "newsletter": ("hebdomadaire", "édition"),
        "finance_domains": ("impots.gouv", "credit-agricole", "societegenerale",
                            "bnpparibas", "labanquepostale", "boursorama"),
    },
    "es": {
        "finance": ("factura", "pago", "extracto", "recibo", "transferencia",
                    "reembolso", "impuestos"),
        "security": ("contraseña", "inicio de sesión", "código de verificación",
                     "alerta de seguridad"),
        "orders": ("pedido", "enviado", "envío", "entrega", "entregado",
                   "devolución", "seguimiento"),
        "travel": ("reserva", "vuelo", "billete", "tarjeta de embarque"),
        "expiry": ("vence", "caduca", "vencimiento", "renovación", "último aviso"),
        "promo": ("descuento", "oferta", "cupón", "rebajas", "gratis"),
        "newsletter": ("boletín", "semanal", "edición"),
        "finance_domains": ("bbva", "caixabank", "bancosantander", "sabadell",
                            "agenciatributaria"),
    },
}

FAMILIES = ("finance", "security", "orders", "travel", "expiry", "promo", "newsletter")


@dataclass(frozen=True)
class Patterns:
    finance: re.Pattern
    security: re.Pattern
    orders: re.Pattern
    travel: re.Pattern
    expiry: re.Pattern
    promo: re.Pattern
    newsletter: re.Pattern
    finance_domains: tuple[str, ...]


_PATTERN_CACHE: dict[tuple[str, ...], Patterns] = {}


def patterns(locales=None) -> Patterns:
    """Compiled keyword families for the enabled packs (all of them if none)."""
    chosen = tuple(sorted(l for l in (locales or ()) if l in LOCALES)) or tuple(LOCALES)
    if chosen not in _PATTERN_CACHE:
        words = {f: [] for f in (*FAMILIES, "finance_domains")}
        for loc in chosen:
            for fam, ws in LOCALES[loc].items():
                for w in ws:
                    if w not in words[fam]:
                        words[fam].append(w)
        _PATTERN_CACHE[chosen] = Patterns(
            **{f: _kw(*(words[f] or ["(?!x)x"])) for f in FAMILIES},
            finance_domains=tuple(words["finance_domains"]),
        )
    return _PATTERN_CACHE[chosen]


_ALL = patterns()
# The full-pack patterns under their old names, for anything importing them.
FINANCE_RE = _ALL.finance
SECURITY_RE = _ALL.security
ORDER_RE = _ALL.orders
TRAVEL_RE = _ALL.travel
EXPIRY_RE = _ALL.expiry
PROMO_RE = _ALL.promo
NEWSLETTER_RE = _ALL.newsletter
FINANCE_HINT_DOMAINS = _ALL.finance_domains

DEV_DOMAINS = {
    "github.com", "gitlab.com", "bitbucket.org", "circleci.com", "travis-ci.org",
    "vercel.com", "netlify.com", "npmjs.com", "pypi.org", "docker.com",
    "sentry.io", "datadoghq.com", "pagerduty.com", "amazonaws.com",
    "cloud.google.com", "digitalocean.com", "heroku.com", "atlassian.com",
    "atlassian.net", "linear.app", "render.com", "cloudflare.com",
}
SOCIAL_DOMAINS = {
    "linkedin.com", "facebookmail.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "quora.com", "reddit.com", "pinterest.com", "meetup.com",
    "tiktok.com", "threads.net", "discord.com", "youtube.com",
}
WORK_ISH_DOMAINS = {"slack.com", "notion.so", "zoom.us", "asana.com", "figma.com"}


@dataclass
class Verdict:
    category: str
    subcategory: str
    attention: str
    retention: str
    protected: bool
    confidence: float
    reasons: list[str]


def classify(m: dict, locales=None, correspondents=None) -> Verdict:
    """`m` is a normalized email row dict (see sync.normalize).

    `locales` picks the keyword packs (default: all). `correspondents` is the
    set of addresses the account owner has sent mail to; mail from them is
    treated as a real conversation.
    """
    P = patterns(locales)
    FINANCE_RE, SECURITY_RE, ORDER_RE = P.finance, P.security, P.orders
    TRAVEL_RE, EXPIRY_RE, PROMO_RE = P.travel, P.expiry, P.promo
    NEWSLETTER_RE, FINANCE_HINT_DOMAINS = P.newsletter, P.finance_domains
    subject = (m.get("subject") or "")
    domain = (m.get("from_domain") or "").lower()
    local = (m.get("from_email") or "").split("@")[0].lower()
    labels = [l.lower() for l in (m.get("labels") or [])]
    has_list = bool(m.get("list_id")) or bool(m.get("unsubscribe"))
    reasons: list[str] = []
    conf = 0.5

    # Gmail's category labels, and the equally-named folders people keep on
    # other servers, are a strong hint about what a message is.
    tagged_promo = any("promotion" in l for l in labels)
    tagged_social = any("social" in l for l in labels)
    tagged_forum = any("forum" in l for l in labels)
    starred = bool(m.get("is_starred"))
    important = bool(m.get("is_important"))

    automated = any(local == a or local.startswith(a + "-") or local.startswith(a + ".")
                    for a in AUTOMATED_LOCALPARTS) or "noreply" in local or "no-reply" in local
    root = ".".join(domain.split(".")[-2:]) if domain else ""

    def dom_in(names) -> bool:
        return domain in names or root in names

    category, sub = "unknown", ""

    # -- category ----------------------------------------------------------
    if local in ("mailer-daemon", "postmaster"):
        # Bounces mention "delivery" and "failure"; they are not orders.
        category, sub, conf = "automated", "bounce", 0.9
        reasons.append("mail system notice")
    elif SECURITY_RE.search(subject):
        category, sub, conf = "security", _sec_sub(subject), 0.9
        reasons.append("security wording in subject")
    elif FINANCE_RE.search(subject) or any(h in domain for h in FINANCE_HINT_DOMAINS):
        if PROMO_RE.search(subject) and not FINANCE_RE.search(subject):
            category, sub, conf = "promotion", "", 0.7
            reasons.append("promotional wording from a finance domain")
        else:
            category, sub, conf = "finance", _fin_sub(subject), 0.85
            reasons.append("finance wording or known finance domain")
    elif TRAVEL_RE.search(subject):
        category, sub, conf = "travel", _travel_sub(subject), 0.8
        reasons.append("travel wording in subject")
    elif ORDER_RE.search(subject):
        category, sub, conf = "orders", _order_sub(subject), 0.8
        reasons.append("order wording in subject")
    elif dom_in(DEV_DOMAINS):
        category, sub, conf = "developer", root, 0.85
        reasons.append("known developer service")
    elif dom_in(SOCIAL_DOMAINS):
        category, sub, conf = "social", root, 0.85
        reasons.append("known social network")
    elif dom_in(WORK_ISH_DOMAINS):
        category, sub, conf = "work", root, 0.7
        reasons.append("known work tool")
    elif tagged_promo or PROMO_RE.search(subject):
        category, conf = "promotion", 0.7
        reasons.append("promotional signals")
    elif has_list and NEWSLETTER_RE.search(subject):
        category, conf = "newsletter", 0.8
        reasons.append("mailing-list headers + newsletter wording")
    elif has_list:
        category, conf = "newsletter", 0.65
        reasons.append("List-Id / List-Unsubscribe header")
    elif tagged_social or tagged_forum:
        category, conf = "social", 0.6
        reasons.append("social/forums label or folder")
    elif automated:
        category, conf = "automated", 0.6
        reasons.append("automated sender address")
    elif not has_list and not automated:
        category, conf = "human", 0.6
        reasons.append("no list headers, personal-looking sender")

    if category in ("human", "unknown") and important:
        conf = min(conf + 0.1, 0.95)

    # Someone you have written to is a correspondent, whatever their address
    # looks like. Mailing lists are excluded: replying to a list once does not
    # make every post on it a conversation.
    sender = (m.get("from_email") or "").lower()
    replied = bool(correspondents) and sender in correspondents and not has_list
    if replied and category in ("automated", "unknown"):
        category, conf = "human", 0.75

    # -- protection --------------------------------------------------------
    protected = category in ("finance", "security", "travel", "human") or (
        category == "orders" and not PROMO_RE.search(subject)
    )
    if replied:
        protected = True
        reasons.append("you have written to this sender")
    if starred:
        protected = True
        reasons.append("starred/flagged by you")
    if category in ("newsletter", "promotion", "social") and not (starred or replied):
        protected = False

    # -- attention ---------------------------------------------------------
    unread = bool(m.get("is_unread"))
    inbox = bool(m.get("is_inbox"))
    if category == "security" and unread:
        attention = ACTION_REQUIRED
    elif EXPIRY_RE.search(subject) and unread:
        attention = ACTION_REQUIRED
        reasons.append("deadline / expiry wording")
    elif category in ("finance", "travel") and unread:
        attention = POTENTIALLY_IMPORTANT
    elif (category == "human" or replied) and unread and inbox:
        attention = POTENTIALLY_IMPORTANT
    elif starred or (important and unread):
        attention = POTENTIALLY_IMPORTANT
    elif category in ("orders", "work", "developer") and unread and inbox:
        attention = READ_LATER
    elif unread and category in ("newsletter",):
        attention = READ_LATER
    elif category in ("promotion", "social", "automated"):
        attention = NO_ATTENTION
    else:
        attention = INFORMATIONAL

    # Something with a deadline that you have not read yet stays out of bulk
    # actions until you have seen it - unless it is plainly marketing.
    if attention == ACTION_REQUIRED and category not in ("promotion", "newsletter", "social"):
        if not protected:
            reasons.append("unread and time-sensitive")
        protected = True

    # -- retention ---------------------------------------------------------
    age_days = m.get("age_days", 0) or 0
    if protected:
        retention = PROTECTED
    elif category in ("promotion", "social") and age_days > 90:
        retention = CLEANUP
    elif category == "newsletter" and unread and age_days > 180:
        retention = CLEANUP
    elif category in ("promotion", "social", "automated"):
        retention = ARCHIVE if age_days > 30 else REVIEW
    elif category in ("developer", "work"):
        retention = KEEP if age_days < 365 else ARCHIVE
    elif category == "newsletter":
        retention = ARCHIVE if age_days > 60 else REVIEW
    else:
        retention = REVIEW

    # A one-time code is only interesting while it is still valid.
    if category == "security" and sub == "otp" and age_days > 7:
        protected = False
        attention = INFORMATIONAL
        retention = CLEANUP if age_days > 30 else REVIEW
        reasons.append("expired one-time code")

    return Verdict(category, sub, attention, retention, protected, round(conf, 2), reasons)


def _sec_sub(s: str) -> str:
    low = s.lower()
    if re.search(r"\botp\b|one-time|passcode|verification code", low):
        return "otp"
    if "password" in low:
        return "password"
    if "sign" in low or "login" in low or "log in" in low:
        return "login alert"
    return "account"


def _fin_sub(s: str) -> str:
    low = s.lower()
    for key, name in (
        ("invoice", "invoice"), ("receipt", "receipt"), ("statement", "statement"),
        ("salary", "salary"), ("payslip", "salary"), ("tax", "tax"), ("gst", "tax"),
        ("premium", "insurance"), ("mutual fund", "investment"),
        ("portfolio", "investment"), ("dividend", "investment"),
        ("credited", "payment"), ("debited", "payment"), ("payment", "payment"),
        ("bill", "bill"), ("emi", "loan"),
    ):
        if key in low:
            return name
    return "finance"


def _travel_sub(s: str) -> str:
    low = s.lower()
    if "flight" in low or "boarding" in low or "pnr" in low:
        return "flight"
    if "hotel" in low or "stay" in low:
        return "hotel"
    return "booking"


def _order_sub(s: str) -> str:
    low = s.lower()
    if "deliver" in low:
        return "delivery"
    if "ship" in low or "dispatch" in low or "tracking" in low:
        return "shipping"
    if "return" in low or "refund" in low:
        return "return"
    return "order"


def apply_rules(verdict: Verdict, rules: list[dict], m: dict) -> Verdict:
    """User rules win over the heuristics."""
    sender = (m.get("from_email") or "").lower()
    domain = (m.get("from_domain") or "").lower()
    list_id = (m.get("list_id") or "").lower()
    for r in rules:
        value = (r["match_value"] or "").lower()
        target = {"sender": sender, "domain": domain, "list_id": list_id}[r["match_type"]]
        if not value or value not in target:
            continue
        if r["action"] == "protect":
            verdict.protected = True
            verdict.retention = PROTECTED
            verdict.reasons.append(f"rule: protect {r['match_type']} {value}")
        elif r["action"] == "ignore":
            verdict.protected = False
            verdict.attention = NO_ATTENTION
            verdict.retention = CLEANUP
            verdict.reasons.append(f"rule: ignore {r['match_type']} {value}")
        elif r["action"] == "category" and r["category"]:
            verdict.category = r["category"]
            verdict.confidence = 1.0
            verdict.reasons.append(f"rule: category {r['category']}")
    return verdict
