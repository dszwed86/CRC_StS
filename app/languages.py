"""Language codes supported by the Palabra Speech-to-Speech API.

Source (from https://platform.palabra.ai/docs/speech-to-speech/supported-languages).
"""

from __future__ import annotations

_NAMES: dict[str, str] = {
    "ar": "Arabic", "ar-sa": "Arabic (Saudi Arabia)", "ar-ae": "Arabic (UAE)",
    "hy": "Armenian", "az": "Azerbaijani", "be": "Belarusian", "bn": "Bengali",
    "bg": "Bulgarian", "yue": "Cantonese", "ca": "Catalan",
    "zh": "Chinese", "zh-hans": "Chinese (Simplified)", "zh-hant": "Chinese (Traditional)",
    "hr": "Croatian", "cs": "Czech", "da": "Danish", "nl": "Dutch", "en": "English",
    "et": "Estonian", "fil": "Filipino", "fi": "Finnish", "fr": "French", "fr-ca": "French (Canada)",
    "gl": "Galician", "de": "German", "el": "Greek", "he": "Hebrew", "hi": "Hindi",
    "hu": "Hungarian", "id": "Indonesian", "ga": "Irish", "it": "Italian", "ja": "Japanese",
    "kk": "Kazakh", "ko": "Korean", "lv": "Latvian", "lt": "Lithuanian", "mk": "Macedonian",
    "ms": "Malay", "mt": "Maltese", "mr": "Marathi", "mn": "Mongolian", "no": "Norwegian",
    "fa": "Persian", "pl": "Polish", "pt": "Portuguese", "pt-br": "Portuguese (Brazil)",
    "ro": "Romanian", "ru": "Russian", "sr": "Serbian", "sk": "Slovak", "sl": "Slovenian",
    "es": "Spanish", "es-ar": "Spanish (Argentina)", "es-ch": "Spanish (Chile)",
    "es-co": "Spanish (Colombia)", "es-la": "Spanish (Latin America)", "es-mx": "Spanish (Mexico)",
    "sw": "Swahili", "sv": "Swedish", "ta": "Tamil", "th": "Thai", "tr": "Turkish",
    "uk": "Ukrainian", "ur": "Urdu", "ug": "Uyghur", "vi": "Vietnamese", "cy": "Welsh",
}

SOURCE_CODES = [
    "ar", "hy", "be", "bn", "bg", "yue", "ca", "zh", "hr", "cs", "da", "nl", "en", "et",
    "fil", "fi", "fr", "gl", "de", "el", "he", "hi", "hu", "id", "ga", "it", "ja", "kk",
    "ko", "lv", "lt", "mk", "ms", "mt", "mr", "mn", "no", "fa", "pl", "pt", "ro", "ru",
    "sr", "sk", "sl", "es", "sw", "sv", "ta", "th", "tr", "uk", "ur", "ug", "vi", "cy",
]

TARGET_CODES = [
    "ar", "ar-sa", "ar-ae", "hy", "az", "be", "bg", "zh-hans", "zh-hant", "hr", "cs", "da",
    "nl", "en", "et", "fil", "fi", "fr", "fr-ca", "de", "el", "he", "hi", "hu", "id", "it",
    "ja", "kk", "ko", "ms", "mt", "no", "pl", "pt", "pt-br", "ro", "ru", "sr", "sk", "sl",
    "es", "es-ar", "es-ch", "es-co", "es-la", "es-mx", "sv", "ta", "tr", "uk", "ur", "vi", "cy",
]


def name(code: str) -> str:
    return _NAMES.get(code, code)


SOURCE_LANGUAGES: list[tuple[str, str]] = [(c, name(c)) for c in SOURCE_CODES]
TARGET_LANGUAGES: list[tuple[str, str]] = [(c, name(c)) for c in TARGET_CODES]

DEFAULT_SOURCE = "pl"
DEFAULT_TARGET = "en"
