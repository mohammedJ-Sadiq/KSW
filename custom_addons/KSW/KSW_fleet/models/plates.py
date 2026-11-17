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


# Latin → the letter Absher writes. On the plate each Latin letter sits under
# its Arabic one, so read left to right the Latin letters come out reversed:
# «أ ص ر» = "RXA" (as printed on the istimara, and in prod's "ZTB-8617").
_ARABIC = {'A': 'أ', 'B': 'ب', 'J': 'ح', 'D': 'د', 'R': 'ر', 'S': 'س', 'X': 'ص', 'T': 'ط', 'E': 'ع',
           'G': 'ق', 'K': 'ك', 'L': 'ل', 'Z': 'م', 'N': 'ن', 'H': 'ه', 'U': 'و', 'V': 'ي'}


def _plate_parts(text):
    """(Arabic letters in Absher's order, digits) from a plate in either script."""
    part = next((p for p in re.split(r'[/|\n]', text or '') if _one_plate_key(p)), '')
    part = part.translate(_DIGITS).upper()
    digits = ''.join(re.findall(r'\d', part))
    letters = [ch for ch in part if ch.isalpha()]
    # A real plate: 1-3 plate letters (all from one script) and 1-4 digits.
    # Anything else ("TEST-120", a fleet name) is left as typed.
    if not (1 <= len(letters) <= 3 and 1 <= len(digits) <= 4):
        return [], ''
    if all(ch in _LETTERS for ch in letters):
        return letters, digits
    if all(ch in _ARABIC for ch in letters):
        return [_ARABIC[ch] for ch in reversed(letters)], digits
    return [], ''


def plate_arabic(text):
    """«أ ص ر 8935», whatever script the plate was typed in."""
    letters, digits = _plate_parts(text)
    return ' '.join(letters + [digits]).strip() if digits else ''


def plate_english(text):
    """"RXA 8935", whatever script the plate was typed in."""
    letters, digits = _plate_parts(text)
    latin = ''.join(_LETTERS[ch] for ch in reversed(letters))
    return ('%s %s' % (latin, digits)).strip() if digits else ''


def vin_key(text):
    return re.sub(r'[^A-Z0-9]', '', (text or '').upper())
