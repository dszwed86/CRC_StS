"""Minimal PL -> EN UI translation.

The whole app is written in Polish; tr() looks up the current language's
translation for a Polish source string, falling back to the Polish
original for anything not yet translated -- so a missed string degrades
to Polish instead of crashing or showing a raw translation key. Language
switches take effect on the next launch (this app has no
QTranslator/retranslateUi wiring to re-render already-built widgets live).
"""

LANG_PL = "pl"
LANG_EN = "en"

_current_lang = LANG_PL

_TRANSLATIONS: dict[str, str] = {
    "(nie wybrano pliku)": "(no file selected)",
    "Aktywny w Palabra.": "Active in Palabra.",
    "Audio/Video": "Audio/Video",
    "Brak ID głosu": "No voice ID",
    "Brak aktywnego glosariusza dla tej pary językowej.": "No active glossary for this language pair.",
    "Brak błędów": "No errors",
    "Brak danych": "Missing data",
    "Brak glosariuszy na koncie.": "No glossaries on the account.",
    "Brak klucza": "No key",
    "Brak klucza API": "No API key",
    "Brak pliku": "No file",
    "Brak transkrypcji": "No transcript",
    "Brak urządzenia wyjściowego": "No output device",
    "Błąd": "Error",
    "Błąd odczytu mikrofonu": "Microphone read error",
    "Błąd odczytu pliku": "File read error",
    "Błąd połączenia": "Connection error",
    "Błąd serwera": "Server error",
    "Błąd testu wyjścia": "Output test error",
    "Błąd urządzenia audio": "Audio device error",
    "Błąd uwierzytelniania": "Authentication error",
    "Błąd zapisu": "Save error",
    "Cień pod tekstem": "Text shadow",
    "Czcionka:": "Font:",
    "Dodaj": "Add",
    "Domyślny (auto)": "Default (auto)",
    "Dostraja tłumaczenie pod rejestr kazań/treści religijnych (Palabra: style="
    "church_catholic) -- np. poprawnie oddaje idiomy biblijne i liczebniki, zamiast"
    " dosłownego tłumaczenia słowo w słowo. Zmierzone: bez dodatkowego opóźnienia."
    " Zmienia znaczną część zdań stylistycznie, więc wyłącz dla świeckich sesji.": (
        "Tunes the translation toward a sermon/religious register (Palabra: style="
        "church_catholic) -- e.g. correctly renders biblical idioms and numerals"
        " instead of a literal word-for-word translation. Measured: no added"
        " latency. Rewords a significant share of sentences stylistically, so"
        " turn it off for secular sessions."
    ),
    "Dostępna nowa wersja": "New version available",
    "Dźwięk cichszy niż ten poziom jest całkowicie pomijany (zamieniany na ciszę) "
    "zanim trafi do tłumaczenia — Twoja mowa musi być głośniejsza niż ustawiony próg, "
    "żeby się liczyła.\n\n"
    "0% (Wyłączony) = nic nie jest pomijane, wszystko przechodzi normalnie.\n"
    "Im wyżej, tym WIĘCEJ dźwięku jest odcinane (nie odwrotnie) — przy wysokiej "
    "wartości nawet Twoja własna, cichsza mowa może zostać ucięta.\n\n"
    "Przydatne głównie przeciw pętli sprzężenia zwrotnego (mikrofon łapiący własne "
    "tłumaczenie z głośnika) — zacznij od niskiej wartości (15-20%) i zwiększaj tylko "
    "jeśli to konieczne.": (
        "Sound quieter than this level is completely skipped (replaced with silence) "
        "before it reaches translation — your speech must be louder than the set "
        "threshold to count.\n\n"
        "0% (Off) = nothing is skipped, everything passes through normally.\n"
        "The higher you go, the MORE sound gets cut (not the other way around) — at a "
        "high value even your own, quieter speech may get cut off.\n\n"
        "Mainly useful against audio feedback loops (mic picking up its own "
        "translation from a speaker) — start with a low value (15-20%) and increase "
        "only if needed."
    ),
    "Glosariusz": "Glossary",
    "Glosariusz...": "Glossary...",
    "Gotowy": "Ready",
    "Głos:": "Voice:",
    "Głośność mikrofonu:": "Microphone volume:",
    "Historia sesji": "Session history",
    "Historia sesji...": "Session history...",
    "ID głosu skopiuj z portalu app.palabra.ai/voices (zakładka biblioteki głosów"
    " lub klonowanie).": (
        "Copy the voice ID from the app.palabra.ai/voices portal (voice library "
        "or cloning tab)."
    ),
    "ID głosu z app.palabra.ai/voices": "Voice ID from app.palabra.ai/voices",
    "Ignoruj ciszej niż:": "Ignore quieter than:",
    "Inny (ID z portalu Palabra)...": "Other (ID from the Palabra portal)...",
    "Jeszcze żaden błąd nie został zapisany -- plik errors.log nie istnieje.": (
        "No error has been logged yet -- the errors.log file doesn't exist."
    ),
    "Język aplikacji:": "App language:",
    "Język docelowy:": "Target language:",
    "Język źródłowy:": "Source language:",
    "Kanał": "Channel",
    "Klonowanie głosu mówcy (eksperymentalne)": "Speaker voice cloning (experimental)",
    "Klucz API Palabra:": "Palabra API key:",
    "Klucz API działa poprawnie.": "The API key works correctly.",
    "Klucz API z platform.palabra.ai/api-keys": "API key from platform.palabra.ai/api-keys",
    "Kolor tekstu": "Text color",
    "Kolor tekstu:": "Text color:",
    "Kolor tła": "Background color",
    "Kolor tła:": "Background color:",
    "Kończenie sesji (np. wolne połączenie) nie zdążyło się zakończyć.\n\n"
    "Zamknięcie teraz może zostawić mikrofon/słuchawki zajęte, dopóki "
    "aplikacja nie dokończy zwalniania urządzenia w tle — sprawdź Menedżera "
    "zadań (python.exe), jeśli dźwięk przestanie działać.\n\n"
    "Zamknąć mimo to?": (
        "Ending the session (e.g. a slow connection) didn't finish in time.\n\n"
        "Closing now may leave the microphone/headset busy until the app finishes "
        "releasing the device in the background — check Task Manager (python.exe) "
        "if audio stops working.\n\n"
        "Close anyway?"
    ),
    "Który kanał wejściowy tego urządzenia nagrywać (dla interfejsów z więcej niż jednym wejściem).": (
        "Which input channel of this device to record (for interfaces with more than one input)."
    ),
    "Lista jest pusta -- glosariusz wyłączony dla tej pary językowej.": (
        "The list is empty -- glossary disabled for this language pair."
    ),
    "Mikrofon:": "Microphone:",
    "Nazwa (np. Lektor)": "Name (e.g. Narrator)",
    "Nie ma jeszcze żadnej transkrypcji do zapisania.": "There's no transcript to save yet.",
    "Nie udało się odtworzyć dźwięku testowego": "Couldn't play the test tone",
    "Nie udało się przełączyć mikrofonu": "Couldn't switch the microphone",
    "Nie udało się ustawić pliku": "Couldn't set the file",
    "Nie udało się zapisać pliku": "Couldn't save the file",
    "Nie wykryto wirtualnego kabla audio (VB-Cable / BlackHole)."
    " Zainstaluj go, aby OBS mógł odebrać tłumaczenie — patrz README.": (
        "No virtual audio cable detected (VB-Cable / BlackHole). Install one so OBS "
        "can pick up the translation — see the README."
    ),
    "Nieoczekiwany błąd": "Unexpected error",
    "Nieprawidłowe saldo": "Invalid balance",
    "Nieprawidłowy klucz API": "Invalid API key",
    "Nieprawidłowy plik": "Invalid file",
    "Nieprzezroczystość tła:": "Background opacity:",
    'Niezapisane zmiany -- kliknij "Zapisz w Palabra", żeby zaczęły obowiązywać.': (
        'Unsaved changes -- click "Save to Palabra" for them to take effect.'
    ),
    "Odczep okienko z tłumaczeniem": "Detach translation window",
    "Odebrane przetłumaczone audio nie jest odtwarzane na wybrane wyjście -- zostaje "
    "tylko tekst (log/overlay). Palabra API nie oferuje trybu bez syntezy mowy, więc "
    "koszt sesji się nie zmienia -- to tylko wycisza odtwarzanie po stronie aplikacji.": (
        "Received translated audio isn't played on the selected output -- only the "
        "text remains (log/overlay). The Palabra API has no speech-generation-off "
        "mode, so the session cost doesn't change -- this only mutes playback on the "
        "app's side."
    ),
    "Odtwarza krótki dźwięk testowy na wybrane urządzenie wyjściowe -- "
    "bez uruchamiania sesji Palabra, więc bez kosztu -- przydatne do sprawdzenia, "
    "czy dźwięk faktycznie dociera do tego urządzenia.": (
        "Plays a short test tone on the selected output device -- without starting "
        "a Palabra session, so no cost -- useful for checking that audio is actually "
        "reaching that device."
    ),
    "Odśwież": "Refresh",
    "Odśwież urządzenia": "Refresh devices",
    "Ostrzeżenie": "Warning",
    "Otwiera ~/.sts_bridge/errors.log w domyślnym edytorze tekstu.": (
        "Opens ~/.sts_bridge/errors.log in the default text editor."
    ),
    "Otwórz log błędów": "Open error log",
    "Otwórz panel Palabra (saldo, użycie)": "Open Palabra dashboard (balance, usage)",
    "Para językowa": "Language pair",
    "Pauza": "Pause",
    "Pauza pliku": "Pause file",
    "Plik (opcjonalnie):": "File (optional):",
    "Pliki tekstowe": "Text files",
    "Podaj ID głosu (z app.palabra.ai/voices) albo wybierz inną opcję.": (
        "Enter a voice ID (from app.palabra.ai/voices) or pick another option."
    ),
    "Podaj klucz API przed zapisaniem.": "Enter an API key before saving.",
    "Podaj nazwę i ID głosu.": "Enter a name and a voice ID.",
    "Podaj słowo źródłowe i jego tłumaczenie.": "Enter a source word and its translation.",
    "Podgląd tłumaczenia": "Translation preview",
    "Pokazuje wszystkie glosariusze zapisane na koncie Palabra (nie tylko dla tej pary"
    " językowej) i pozwala usunąć dowolny z nich -- przydatne, jeśli jakiś pozostał"
    " aktywny mimo utraty lokalnego zapisu w tej aplikacji.": (
        "Shows every glossary saved on the Palabra account (not just for this language"
        " pair) and lets you delete any of them -- useful if one stayed active despite"
        " losing the local record in this app."
    ),
    "Pokaż w logu:": "Show in log:",
    "Pokaż:": "Show:",
    "Poziom dźwięku faktycznie odtwarzanego na wybrane wyjście, na żywo, w trakcie "
    "trwającej sesji -- potwierdza, że przetłumaczone audio realnie dociera do "
    "urządzenia (np. wirtualnego kabla), a nie tylko że zostało odebrane.": (
        "The level of audio actually being played to the selected output, live, "
        "during an active session -- confirms the translated audio is really "
        "reaching the device (e.g. a virtual cable), not just that it was received."
    ),
    "Poziom dźwięku odbieranego z mikrofonu na żywo, w trakcie trwającej sesji -- "
    "potwierdza, że mikrofon faktycznie łapie dźwięk, niezależnie od Głośności mikrofonu.": (
        "The level of audio being received from the microphone live, during an "
        "active session -- confirms the mic is actually picking up sound, "
        "independent of the Microphone volume setting."
    ),
    "Poziom sygnału:": "Signal level:",
    "Poziom wyjścia:": "Output level:",
    "Przewija plik o": "Skips the file by",
    "Połączenie przerwane": "Connection dropped",
    "Przekroczono czas oczekiwania na odpowiedź serwera.": "Timed out waiting for the server's response.",
    "Razem": "Total",
    "Saldo Palabra (USD, orientacyjne):": "Palabra balance (USD, estimated):",
    "Saldo musi być liczbą, np. 45.00.": "Balance must be a number, e.g. 45.00.",
    "Sprawdzenie": "Checking",
    "Start": "Start",
    "Start pliku": "Start file",
    "Stop": "Stop",
    "Styl kościelny": "Church style",
    "System nie zgłasza żadnego urządzenia audio wyjściowego.": "The system reports no audio output device.",
    "Szacunkowe saldo w USD. Palabra nie udostępnia prawdziwego salda przez API, więc "
    "to tylko przybliżenie liczone przez aplikację (odejmuje szacowany koszt każdej "
    "sesji) -- może się rozjechać z rzeczywistością. Wpisz tu aktualną wartość z panelu "
    "Palabra, żeby zsynchronizować.": (
        "Estimated balance in USD. Palabra doesn't expose the real balance through "
        "its API, so this is only an approximation the app computes itself "
        "(subtracting the estimated cost of each session) -- it can drift from "
        "reality. Enter the current value from the Palabra dashboard here to "
        "resync."
    ),
    "Słowo źródłowe (np. Jehowa)": "Source word (e.g. Jehovah)",
    "Tego nie można cofnąć.": "This cannot be undone.",
    "Testowanie...": "Testing...",
    "Testuj klucz": "Test key",
    "Testuj wyjście": "Test output",
    "Tylko napisy (bez dźwięku)": "Subtitles only (no audio)",
    "Tylko tłumaczenie": "Translation only",
    "Tylko źródłowy": "Source only",
    "Tłumaczenie": "Translation",
    "Tłumaczenie (np. Jehovah)": "Translation (e.g. Jehovah)",
    "Ustaw klucz API w Ustawieniach przed rozpoczęciem.": "Set an API key in Settings before starting.",
    "Ustaw klucz API w Ustawieniach przed zapisem glosariusza.": (
        "Set an API key in Settings before saving the glossary."
    ),
    "Ustaw klucz API w Ustawieniach przed zarządzaniem glosariuszami.": (
        "Set an API key in Settings before managing glossaries."
    ),
    "Ustawienia": "Settings",
    "Ustawienia wyglądu": "Appearance settings",
    "Ustawienia wyglądu overlay...": "Overlay appearance settings...",
    "Ustawienia wyglądu...": "Appearance settings...",
    "Ustawienia...": "Settings...",
    "Usunąć": "Delete",
    "Usunąć całą zapisaną historię sesji? Tego nie można cofnąć.": (
        "Delete the entire saved session history? This cannot be undone."
    ),
    "Usunąć glosariusz?": "Delete glossary?",
    "Usuwanie...": "Deleting...",
    "Usuń wybrany plik": "Remove the selected file",
    "Usuń zaznaczone": "Remove selected",
    "Usuń zaznaczony": "Remove selected",
    "Wczytywanie...": "Loading...",
    "Wpisz klucz API przed testem.": "Enter an API key before testing.",
    "Wstrzymuje/wznawia całą sesję (mikrofon i plik, jeśli jest), niezależnie od stanu pliku. (F6)": (
        "Pauses/resumes the whole session (microphone and file, if any), "
        "independent of the file's own state. (F6)"
    ),
    "Wstrzymuje/wznawia tylko plik -- mikrofon i reszta sesji nie są tym dotknięte.": (
        "Pauses/resumes only the file -- the microphone and the rest of the "
        "session are unaffected."
    ),
    "Wszystkie glosariusze na koncie": "All glossaries on the account",
    "Wszystkie glosariusze na koncie...": "All glossaries on the account...",
    "Wszystkie pliki": "All files",
    "Wybierz plik audio/wideo": "Choose an audio/video file",
    "Wybierz plik audio/wideo do przetłumaczenia.": "Choose an audio/video file to translate.",
    "Wybierz plik...": "Choose file...",
    "Wycisz": "Mute",
    "Wycisza mikrofon bez zmiany ustawionej głośności (skrót: M).": (
        "Mutes the microphone without changing the set volume (shortcut: M)."
    ),
    "Wyczyścić historię?": "Clear history?",
    "Wyczyść historię": "Clear history",
    "Wyczyść transkrypcję": "Clear transcript",
    "Wymusza dokładne tłumaczenie podanych słów/fraz (np. imion biblijnych) zamiast"
    " tego, co Palabra przetłumaczyłaby sama. Dotyczy tylko powyższej pary językowej --"
    " dla innej pary trzeba otworzyć to okno ponownie po jej wybraniu.": (
        "Forces exact translation of the given words/phrases (e.g. biblical names) instead"
        " of whatever Palabra would translate them to on its own. Applies only to the"
        " language pair above -- for a different pair, reopen this window after selecting it."
    ),
    "Wymuś własne tłumaczenie konkretnych słów/imion (np. biblijnych) dla obecnie"
    " wybranej pary językowej -- zamiast tego, co Palabra przetłumaczyłaby sama.": (
        "Force your own translation of specific words/names (e.g. biblical) for the"
        " currently selected language pair -- instead of whatever Palabra would translate"
        " them to on its own."
    ),
    "Wyjście:": "Output:",
    "Wyłączony": "Off",
    "Wznów": "Resume",
    "Zaawansowane": "Advanced",
    "Zamknij": "Close",
    "Zamknij okienko": "Close window",
    "Zamknij okienko z tłumaczeniem": "Close translation window",
    "Zamykanie trwa dłużej niż zwykle": "Closing is taking longer than usual",
    "Zamykanie — kończę sesję...": "Closing — ending session...",
    "Zapisane głosy": "Saved voices",
    "Zapisane głosy...": "Saved voices...",
    "Zapisano w Palabra.": "Saved to Palabra.",
    "Zapisywanie...": "Saving...",
    "Zapisz transkrypcję": "Save transcript",
    "Zapisz transkrypcję...": "Save transcript...",
    "Zapisz w Palabra": "Save to Palabra",
    "Zatrzymywanie...": "Stopping...",
    "Zawsze na wierzchu": "Always on top",
    "Zmiana języka aplikacji będzie widoczna po ponownym uruchomieniu.": (
        "The app language change will take effect after restarting the app."
    ),
    "Zmieniono język": "Language changed",
    "bitów)": "bits)",
    "kliknij, aby otworzyć": "click to open",
    "nie można otworzyć pliku": "couldn't open the file",
    "np. 45.00 -- sprawdź w panelu Palabra": "e.g. 45.00 -- check the Palabra dashboard",
    "obsługiwany jest tylko 16-bitowy WAV (plik ma": "only 16-bit WAV is supported (the file is",
    "plik WAV nie zawiera dźwięku.": "the WAV file contains no audio.",
    "plik nie zawiera ścieżki audio.": "the file contains no audio track.",
    "ponawiam próbę": "retrying",
    "połączenie zostało zerwane": "the connection was dropped",
    "saldo": "balance",
    "sesji": "sessions",
    "transkrypcja.txt": "transcript.txt",
    "wymaga pakietu av: uv add av": "requires the av package: uv add av",
    "włączony": "enabled",
    "wyłączony": "disabled",
    "za": "in",
    "Źródło dźwięku": "Audio source",
    "wersja": "version",
    "Źródłowy i tłumaczenie": "Source and translation",
    # _STATE_LABELS values (see gui.py) -- looked up dynamically via
    # tr(_STATE_LABELS.get(state, ...)), not passed to tr() as a literal,
    # so the extraction pass that generated the block above couldn't find
    # these on its own.
    "Łączenie...": "Connecting...",
    "Rozłączono, ponawiam próbę...": "Disconnected, retrying...",
    "Tłumaczę na żywo": "Translating live",
    "Wstrzymano": "Paused",
    "Zatrzymano": "Stopped",
    # SAMPLE_TEXT (see overlay.py) -- same reason, looked up via tr(SAMPLE_TEXT).
    "To jest przykładowy tekst — tak będzie wyglądać napis.": "This is sample text — this is what a caption will look like.",
}


def set_language(lang: str) -> None:
    global _current_lang
    _current_lang = LANG_EN if lang == LANG_EN else LANG_PL


def get_language() -> str:
    return _current_lang


def tr(text: str) -> str:
    if _current_lang == LANG_EN:
        return _TRANSLATIONS.get(text, text)
    return text
