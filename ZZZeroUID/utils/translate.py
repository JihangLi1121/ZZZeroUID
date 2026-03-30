"""English-to-Chinese translation for international HoYoLab data.

International accounts return English names from all API sources (MYS, ENKA, MINIGG).
This module translates the cached character data to Chinese for consistent rendering.
"""

import json
from pathlib import Path
from typing import Dict, Any, List

from gsuid_core.logger import logger

_TRANSLATION_FILE = Path(__file__).parent / "map" / "en2cn_translation.json"
_TR: Dict[str, Dict[str, str]] = {}

if _TRANSLATION_FILE.exists():
    with open(_TRANSLATION_FILE, encoding="utf-8") as f:
        _TR = json.load(f)


def _strip_test(text: str) -> str:
    """Remove (Test1) and similar test prefixes from names."""
    import re
    return re.sub(r'\(Test\d*\)', '', text).strip()


def _t(category: str, text: str) -> str:
    """Translate a single string. Returns original if no mapping found."""
    return _TR.get(category, {}).get(text, text)


def translate_character_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a character's cached data from English to Chinese.

    Modifies the dict in-place and returns it.
    """
    if not _TR:
        return data

    # Character name
    name = data.get("name_mi18n", "")
    if name:
        cn = _t("characters", name)
        if cn != name:
            data["name_mi18n"] = cn

    # Full name
    full_name = data.get("full_name_mi18n", "")
    if full_name:
        cn = _t("characters", full_name)
        if cn != full_name:
            data["full_name_mi18n"] = cn

    # Camp
    camp = data.get("camp_name_mi18n", "")
    if camp:
        cn = _t("camps", camp)
        if cn != camp:
            data["camp_name_mi18n"] = cn

    # Weapon
    weapon = data.get("weapon")
    if weapon and isinstance(weapon, dict):
        wname = weapon.get("name", "")
        if wname:
            cn = _t("weapons", wname)
            if cn != wname:
                weapon["name"] = cn

        # Weapon properties (main_properties + properties)
        for prop_list_key in ("main_properties", "properties"):
            wprops = weapon.get(prop_list_key)
            if wprops and isinstance(wprops, list):
                for prop in wprops:
                    if isinstance(prop, dict):
                        pname = prop.get("property_name", "")
                        if pname:
                            cn = _t("properties", pname)
                            if cn != pname:
                                prop["property_name"] = cn

    # Equipment/Disk sets
    equips = data.get("equip")
    if equips and isinstance(equips, list):
        for eq in equips:
            if not isinstance(eq, dict):
                continue
            # Disk name (individual piece)
            eq_name = eq.get("name", "")
            if eq_name:
                # Extract set name from piece name (e.g. "Woodpecker Electro [1]" -> "Woodpecker Electro")
                for en_set, cn_set in _TR.get("equipment", {}).items():
                    if eq_name.startswith(en_set):
                        eq["name"] = eq_name.replace(en_set, cn_set)
                        break

            # Suit info
            suit = eq.get("equip_suit")
            if suit and isinstance(suit, dict):
                sname = suit.get("name", "")
                if sname:
                    cn = _t("equipment", sname)
                    if cn != sname:
                        suit["name"] = cn

            # Property names
            for prop_list_key in ("properties", "main_properties"):
                props = eq.get(prop_list_key)
                if props and isinstance(props, list):
                    for prop in props:
                        if isinstance(prop, dict):
                            pname = prop.get("property_name", "")
                            if pname:
                                cn = _t("properties", pname)
                                if cn != pname:
                                    prop["property_name"] = cn

    # Top-level properties (character stats)
    props = data.get("properties")
    if props and isinstance(props, list):
        for prop in props:
            if isinstance(prop, dict):
                pname = prop.get("property_name", "")
                if pname:
                    cn = _t("properties", pname)
                    if cn != pname:
                        prop["property_name"] = cn

    # Strip (Test1) prefixes from all name fields
    for key in ("name_mi18n", "full_name_mi18n"):
        val = data.get(key, "")
        if val and "(Test" in val:
            data[key] = _strip_test(val)
    weapon = data.get("weapon")
    if weapon and isinstance(weapon, dict):
        wname = weapon.get("name", "")
        if wname and "(Test" in wname:
            weapon["name"] = _strip_test(wname)

    return data
