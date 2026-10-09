"""Normalize Nigerian local numbers and international numbers for storage."""

import re


def normalize_phone(value, *, required=False):
    if value is None or value == "":
        return (None, "Enter your phone number" if required else None)
    if not isinstance(value, str) or len(value) > 40:
        return None, "Enter a valid phone number"
    number = re.sub(r"[\s()\-]", "", value)
    if re.fullmatch(r"0[789][0-9]{9}", number):
        number = "+234" + number[1:]
    elif number.startswith("00"):
        number = "+" + number[2:]
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", number):
        return None, "Use a Nigerian mobile number or an international number with country code (e.g. +2349039930006)"
    return number, None
