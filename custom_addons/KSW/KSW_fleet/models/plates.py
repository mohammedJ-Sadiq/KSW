"""Saudi plates come in Arabic (أ ص ر ٨٩٣٥) and Latin (8935 RXA): compare a
key both give the same. Letters by the official mapping, as a sorted set
(the two scripts run in opposite directions), digits as ASCII."""
import re

_LETTERS = {
    'ا': 'A', 'أ': 'A', 'إ': 'A', 'آ': 'A', 'ب': 'B', 'ح': 'J', 'د': 'D', 'ر': 'R', 'س': 'S',
    'ص': 'X', 'ط': 'T', 'ع': 'E', 'ق': 'G', 'ك': 'K', 'ل': 'L', 'م': 'Z', 'ن': 'N', 'ه': 'H',
    'ة': 'H', 'و': 'U', 'ى': 'V', 'ي': 'V',
}
_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')


def plate_key(text):
    if not text:
        return ''
    # The same plate written in both scripts ("8935 RXA / أ ص ر ٨٩٣٥"):
    # one of them is the plate, not both run together.
    for part in re.split(r'[/|\n]', text):
        key = _one_plate_key(part)
        if key:
            return key
    return ''


def _one_plate_key(text):
    text = text.translate(_DIGITS).upper()
    digits = ''.join(re.findall(r'\d', text))
    letters = [_LETTERS.get(ch, ch) for ch in text if ch.isalpha()]
    letters = [ch for ch in letters if 'A' <= ch <= 'Z']
    return digits + ''.join(sorted(letters)) if digits else ''


def vin_key(text):
    return re.sub(r'[^A-Z0-9]', '', (text or '').upper())
