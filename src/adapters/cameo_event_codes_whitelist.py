"""CAMEO event codes whitelist for financial/economic event filtering.

This whitelist defines CAMEO event codes relevant to financial markets
and macro-economic analysis. Used by GdeltAdapter's Layer B filtering.

Design:
- Codes are structured with root codes (2-digit), parent codes (3-digit),
  and leaf codes (4-digit) as in the CAMEO taxonomy.
- Matching follows exact prefix logic: e.g. code "163" in whitelist
  matches "163" in Events Data but NOT "16" or "1631".
- The whitelist covers: trade policy, sanctions, economic cooperation,
  financial aid, market regulation, monetary policy, and geopolitical
  events that impact financial markets.

See Also:
    ``data/codebooks/cameo_event_codes.json`` — full CAMEO codebook
"""

# ── Financial/Economic CAMEO Event Codes ─────────────────────────────

CAMEO_EVENT_CODES_WHITELIST: set[str] = {
    # 01 — MAKE PUBLIC STATEMENT (financial/economic related)
    "014",  # Consider policy option
    # 02 — APPEAL (economic cooperation)
    "0211",  # Appeal for economic cooperation
    "0231",  # Appeal for economic aid
    # 03 — EXPRESS INTENT TO COOPERATE (economic)
    "0311",  # Express intent to cooperate economically
    "0331",  # Express intent to provide economic aid
    "0354",  # Express intent to ease economic sanctions
    # 04 — CONSULT (economic/negotiation)
    "046",   # Engage in negotiation
    # 05 — ENGAGE IN DIPLOMATIC COOPERATION (broad cooperation, includes trade deals)
    "051",   # Praise or endorse (markets react to endorsements)
    "052",   # Defend verbally
    "054",   # Rally support for policy change
    # 06 — ENGAGE IN MATERIAL COOPERATION
    "061",   # Cooperate economically
    "062",   # Cooperate on trade
    "0621",  # Provide economic aid
    "0622",  # Provide military aid
    "0623",  # Provide humanitarian aid
    "063",   # Cooperate on judicial matters
    "0641",  # Cooperate on intelligence
    # 07 — PROVIDE AID (economic)
    "071",   # Provide economic aid
    "072",   # Provide military aid
    "073",   # Provide humanitarian aid
    "074",   # Provide military protection
    # 08 — YIELD (economic concessions)
    "081",   # Ease administrative sanctions
    "082",   # Ease political dissent
    "083",   # Accede to release
    "084",   # Ease economic sanctions
    "085",   # Allow international involvement
    "086",   # De-escalate military engagement
    "087",   # Return to negotiations
    # 09 — INVESTIGATE (policy/financial)
    "093",   # Investigate war crimes
    "0941",  # Investigate economic crimes ← currently done with code only; using rough mapping
    # 10 — DEMAND (economic/policy)
    "1031",  # Demand economic cooperation
    "1032",  # Demand military cooperation
    "1033",  # Demand judicial cooperation
    "1034",  # Demand intelligence cooperation
    "104",   # Demand policy change
    "105",   # Demand withdrawal
    "106",   # Demand meeting/negotiation
    "1061",  # Demand negotiation on trade
    "107",   # Demand ceasefire (markets react)
    # 11 — DISAPPROVE
    "111",   # Criticize or denounce (market-moving rhetoric)
    "112",   # Accuse of human rights violations (sanctions trigger)
    "1121",  # Accuse of economic crimes
    "1122",  # Accuse of human rights abuses
    "1123",  # Accuse of aggressive military action
    "1124",  # Accuse of illegal activities
    "1125",  # Accuse of corruption
    "113",   # Rally opposition
    "114",   # Complain officially
    "115",   # Bring lawsuit (regulatory risk)
    "116",   # Object to policies
    "117",   # Object to international involvement
    # 12 — THREATEN (economic/military)
    "121",   # Threaten to impose sanctions/embargo
    "1211",  # Threaten to halt economic aid
    "1212",  # Threaten to halt military aid
    "1213",  # Threaten to halt humanitarian aid
    "122",   # Threaten to impose political sanctions
    "123",   # Threaten to use military force (geopolitical risk)
    "124",   # Threaten violent repression
    "125",   # Threaten to halt negotiations
    "126",   # Threaten to halt mediation
    "127",   # Threaten to halt international involvement
    # 13 — THREATEN WITH FORCE (direct market impact)
    "138",   # Threaten to use military force
    "1381",  # Threaten blockade
    "1382",  # Threaten occupation
    "1384",  # Threaten conventional attack
    "139",   # Give ultimatum
    # 14 — PROTEST (economic/political)
    "141",   # Demonstrate or rally
    "142",   # Hunger strike
    "143",   # Strike or boycott
    "1431",  # Strike/boycott for economic policy
    "144",   # Obstruct passage, block
    "145",   # Violent protest, riot (market risk)
    # 15 — EXHIBIT FORCE POSTURE
    "151",   # Increase police alert
    "152",   # Increase military alert
    "153",   # Mobilize police
    "154",   # Mobilize armed forces
    # 16 — REDUCE RELATIONS (HIGH FINANCIAL IMPACT)
    "161",   # Reduce or break diplomatic relations
    "162",   # Reduce or stop aid
    "1621",  # Reduce/stop economic assistance
    "1622",  # Reduce/stop military assistance
    "163",   # SANCTIONS, EMBARGO ← Critical for financial markets
    "164",   # Halt negotiations
    "165",   # Halt mediation
    "166",   # Expel or withdraw
    # 17 — COERCE
    "171",   # Seize or damage property
    "1711",  # Confiscate property
    "1712",  # Destroy property (supply chain impact)
    "1721",  # Restrict political freedoms
    "1722",  # Ban political parties
    "1723",  # Impose curfew (economic disruption)
    "1724",  # State of emergency / martial law
    "173",   # Arrest, detain
    "174",   # Expel or deport
    "175",   # Violent repression
    # 18 — ASSAULT (headline risk)
    "180",   # Unconventional violence
    "185",   # Attempt to assassinate
    "186",   # Assassination
    # 19 — FIGHT (war/conflict risk)
    "190",   # Conventional military force
    "191",   # Blockade
    "192",   # Occupy territory
    "193",   # Small arms
    "194",   # Artillery and tanks
    "195",   # Aerial weapons
    "196",   # Ceasefire violation
    # 20 — MASS VIOLENCE (extreme risk)
    "200",   # Mass violence
    "201",   # Mass expulsion
    "202",   # Mass killings
    "203",   # Ethnic cleansing
}

__all__ = ["CAMEO_EVENT_CODES_WHITELIST"]
