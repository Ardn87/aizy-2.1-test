"""Local logos from the validated mockup, keyed by normalized supplier name."""
import unicodedata
LOGOS = {'bianchi': 'review/logos/bianchi.png', 'cremerielesmartels': 'review/logos/cremerielesmartels.svg', 'delmaitre': 'review/logos/delmaitre.svg', 'lindt': 'review/logos/lindt.png', 'merat': 'review/logos/merat.svg', 'michellod': 'review/logos/michellod.svg', 'pisciculturedevionnaz': 'review/logos/pisciculturedevionnaz.png', 'pitoeuf': 'review/logos/pitoeuf.png', 'culturefood': 'review/logos/culturefood.svg', 'histand': 'review/logos/histand.svg', 'eauxgazeuse': 'review/logos/eauxgazeuse.svg', 'walker': 'review/logos/walker.svg', 'froneri': 'review/logos/froneri.svg', 'zweifel': 'review/logos/zweifel.svg', 'ristorfoods': 'review/logos/ristorfoods.png'}


def supplier_logo(name):
    key = ''.join(c for c in unicodedata.normalize('NFKD', name).lower() if c.isalnum())
    key = {'hiestand': 'histand', 'cremerielesmartel': 'cremerielesmartels', 'eauxgazeuses': 'eauxgazeuse', 'eauxgazeusesamsteinvalais': 'eauxgazeuse'}.get(key, key)
    return LOGOS.get(key, '')
