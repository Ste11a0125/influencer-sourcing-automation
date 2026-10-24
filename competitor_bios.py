"""Conservative local red flags for bios presenting competing commercial services.
Flags are evidence for review, not a definitive claim about the business.
"""
import re

# Personal experiences and commentary are not provider identity claims.
COMMENTARY = re.compile(r"\b(?:review(?:ing|s)?|compar(?:ing|e)|discuss(?:ing)?|talk(?:ing)? about|worked? (?:at|for|with)|used? (?:an?|the)|customer of|experience with|formerly|former|ex[- ]|not (?:an?|your))\b", re.I)
SELF_ID = re.compile(r"\b(?:we(?:'re| are)|i(?:'m| am)|our (?:company|business)|we (?:provide|offer|sell|manage|handle))\b", re.I)
RULES = [
 ('insurance_provider', 'Bio presents an insurance provider or intermediary',
  r'\b(?:insurance\s+(?:broker(?:age)?|agency|company|provider|intermediary)|(?:licensed|regulated|authorised|authorized)\s+insurer|an?\s+insurer)\b'),
 ('utility_provider', 'Bio presents an energy, broadband or utility provider',
  r'\b(?:energy|electricity|gas|broadband|utility|utilities)\s+(?:supplier|provider|company)\b'),
 ('household_management', 'Bio offers to manage customers’ bills or household admin',
  r"\b(?:we|i|our (?:team|service))\s+(?:can\s+)?(?:manage|handle|take care of|sort|pay|switch)\s+(?:all\s+)?(?:of\s+)?your\s+(?:(?:household|home|energy|utility|insurance)\s+)?(?:bills|admin|insurance|utilities|subscriptions)\b"),
 ('household_service', 'Bio advertises a household bill or admin management service',
  r'\b(?:household|home|bill|bills|utility|subscription|insurance)\s+(?:admin\s+)?management\s+(?:service|platform|company|app)\b'),
 ('bundled_services', 'Bio advertises bundled services for running a home',
  r'\b(?:all (?:of )?the services you need to run your home|(?:energy|broadband|insurance|utilities)[^.!?\n]{0,70}\b(?:one bill|one monthly bill|under one roof))\b'),
 ('insurance_sales', 'Bio directly offers customers insurance products or quotes',
  r"\b(?:we (?:provide|offer|sell)|our)\s+(?:(?:affordable|specialist|comprehensive|tailored|home|car|pet|travel|life)\s+){0,3}insurance\b"),
]


def competitor_bio_flags(biography, source_url=''):
    text=str(biography or '')
    flags=[]
    for segment in re.split(r'[\n|;]+|(?<=[.!?])\s+', text):
        segment=segment.strip()
        if not segment:
            continue
        for key,reason,pattern in RULES:
            match=re.search(pattern, segment, re.I)
            if not match:
                continue
            # Suppress references to providers in commentary, unless this same
            # clause explicitly presents the account itself as a provider.
            prefix=segment[:match.start()]
            if COMMENTARY.search(prefix) and not SELF_ID.search(prefix[COMMENTARY.search(prefix).end():]):
                continue
            flags.append({'rule':key,'reason':reason,'supporting_text':segment,'matched_text':match.group(0),'source_url':source_url})
    # Aviva-style service catalogues plus an explicit corporate voice.
    catalogue=sum(bool(re.search(pattern,text,re.I)) for pattern in [r'\binsurance\b',r'\bsavings\b',r'\binvestments\b',r'\bretirement\b'])
    if catalogue>=3 and re.search(r'\b(?:our social team|our services|official account|welcome to)\b',text,re.I) and not COMMENTARY.search(text):
        flags.append({'rule':'financial_service_catalogue','reason':'Bio presents a corporate financial-services offering','supporting_text':text,'matched_text':text,'source_url':source_url})
    return flags
