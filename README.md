# Palabra S2S → OBS Bridge

Desktopowa aplikacja (Windows i macOS), która tłumaczy mowę w czasie rzeczywistym przez
[Palabra Speech-to-Speech API](https://platform.palabra.ai/docs/speech-to-speech/overview)
i odtwarza przetłumaczony dźwięk na wirtualne urządzenie audio, które OBS Studio
przechwytuje jako źródło na scenie.

Źródłem może być mikrofon (na żywo) albo wgrany plik audio/wideo (odtwarzany i
tłumaczony w tempie rzeczywistym, tak jakby ktoś mówił na żywo).

## Wymagania

- Klucz API Palabra — załóż go na https://platform.palabra.ai/api-keys (nowe konta
  dostają $50 darmowego kredytu; S2S kosztuje $0.04/min)
- Wirtualne urządzenie audio, żeby przekazać dźwięk do OBS:
  - **Windows**: [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) (bezpłatny)
  - **macOS**: [BlackHole](https://existential.audio/blackhole/) (bezpłatny, open source; wersja 2ch wystarczy)

Python **nie jest wymagany** do zwykłego korzystania z aplikacji — gotowe wersje poniżej
mają wszystko (łącznie z Pythonem) zaszyte w jednym pliku.

## Instalacja i uruchomienie

Najprościej: pobierz gotową aplikację ze strony wydań —
**[najnowsza wersja](https://github.com/dszwed86/CRC_StS/releases/latest)**.

**Windows**

1. Pobierz **`PalabraS2S.exe`**.
2. Kliknij dwa razy. To wszystko — nic więcej nie trzeba instalować.

**macOS**

1. Pobierz **`PalabraS2S-macOS.zip`** i rozpakuj (dwuklik w Finderze).
2. Przy **pierwszym** uruchomieniu: kliknij prawym przyciskiem na `PalabraS2S.app` →
   **Otwórz** → potwierdź w oknie, które się pojawi. To jednorazowy krok — macOS ostrzega
   w ten sposób przed każdą aplikacją spoza App Store bez płatnego (99$/rok) certyfikatu
   Apple Developer, którego ten projekt nie ma. Od tego momentu `PalabraS2S.app` uruchamia
   się już normalnym podwójnym kliknięciem.

Każda kolejna sesja to już tylko podwójne kliknięcie w pobrany plik — nic się nie instaluje
ani nie pobiera przy starcie.

<details>
<summary>Budowanie ze źródeł / dla programistów</summary>

Skrypty budujące/startowe są podzielone folderami wg systemu — `windows/` i `mac/`.

**Uruchomienie ze źródeł (bez budowania gotowego pliku)**:
- Windows: `windows/install.bat` (raz), potem `windows/run.bat`.
- macOS: `mac/Zainstaluj.command` (raz), potem `mac/Uruchom.command` — automatycznie
  doinstaluje odpowiedniego Pythona (Homebrew albo instalator z python.org, sam czeka aż
  skończysz przechodzić przez jego okienko), rozpoznaje najczęstsze przyczyny błędów
  (brak Xcode Command Line Tools, za stary/za nowy Python dla PySide6) i podpowiada
  dokładne kroki naprawy zamiast zostawiać samą ścianę tekstu błędu pipa.

Manualnie (dowolny system):

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
python -m app.main
```

**Budowanie własnego `.exe`/`.app`** (to właśnie tak powstały pliki z sekcji wydań powyżej):
`windows/build.bat` / `mac/build.sh` — wymaga najpierw `install.bat`/`Zainstaluj.command`,
doinstalowuje PyInstaller i pakuje całą aplikację (Python + biblioteki) w jeden plik.
Wynik ląduje w `dist/`.

</details>

Przy pierwszym uruchomieniu kliknij **Ustawienia...** i wklej klucz API Palabra —
zostanie zapisany lokalnie w `~/.sts_bridge/.env`. W tym samym oknie:

- **Testuj klucz** — realnie sprawdza połączenie z API (bez uruchamiania płatnej sesji
  tłumaczenia) i pokazuje, czy klucz działa, czy jest odrzucany.
- **Otwórz panel Palabra (saldo, użycie)** — otwiera w przeglądarce
  `platform.palabra.ai/api-keys`, gdzie widoczne jest saldo kredytów i historia użycia.
  Palabra nie udostępnia tego w publicznym API, więc apka nie może pokazać salda
  bezpośrednio — to najbliższe dostępne rozwiązanie.

**Błąd o certyfikacie przy łączeniu z API (macOS)** — Python z instalatora python.org nie
podpina się automatycznie pod systemowy magazyn certyfikatów, więc każde połączenie z API
mogło kończyć się błędem w stylu "certificate verify failed". Aplikacja naprawia to sama od
środka (używa zestawu certyfikatów z pakietu `certifi`, ustawianego zanim cokolwiek łączy się
z siecią) — nie trzeba ręcznie uruchamiać `Install Certificates.command` z folderu Pythona.

## Konfiguracja OBS

1. Zainstaluj wirtualny kabel audio (patrz wyżej) i uruchom ponownie OBS, jeśli był otwarty.
2. W OBS: **Źródła → + → Przechwytywanie wejścia audio (Audio Input Capture)**.
3. Jako urządzenie wybierz:
   - Windows: `CABLE Output (VB-Audio Virtual Cable)`
   - macOS: `BlackHole 2ch`
4. W aplikacji, w polu **Wyjście (do OBS)**, wybierz odpowiadające urządzenie
   *wejściowe* dla tego kabla:
   - Windows: `CABLE Input (VB-Audio Virtual Cable)`
   - macOS: `BlackHole 2ch`

   (Aplikacja automatycznie wykrywa i podpowiada urządzenie, jeśli jego nazwa
   zawiera "CABLE" lub "BlackHole"; w przeciwnym razie wybierz je ręcznie z listy.)
5. Miernik poziomu przy nowym źródle w OBS powinien reagować, gdy aplikacja tłumaczy mowę.

## Użycie

1. Wybierz źródło: **Mikrofon** (i konkretne urządzenie z listy) albo **Plik**
   (i wskaż plik audio/wideo).
2. Ustaw język źródłowy i docelowy (domyślnie polski → angielski) oraz opcjonalnie **Głos**:
   - **Domyślny (auto)** — serwer sam dobiera głos.
   - **default_low** / **default_high** — wbudowane głosy ogólne (niższy/wyższy).
   - **Klonowanie głosu mówcy (eksperymentalne)** — tłumaczenie brzmi głosem osoby mówiącej
     do mikrofonu/w pliku; potrzebuje ok. 10–20 sekund mowy, zanim efekt zacznie być słyszalny.
   - **Inny (ID z portalu Palabra)...** — wpisz konkretne ID głosu skonfigurowane w
     [app.palabra.ai/voices](https://app.palabra.ai/voices).

   Palabra nie udostępnia API do pobrania listy dostępnych głosów — trzeba je najpierw
   znaleźć/skopiować ręcznie w portalu. Żeby nie wklejać tego samego ID za każdym razem,
   przycisk **"Zapisane głosy..."** obok pozwala raz dodać ID z własną nazwą (np. "Lektor") —
   od tego momentu taki głos pojawia się od razu na liście wyboru, także po restarcie aplikacji.
   Głos, tak jak języki i urządzenia, jest zablokowany na czas trwania sesji — zmień go przed
   kliknięciem Start.
3. Kliknij **Start**. W dolnym panelu pojawia się na żywo transkrypcja i tłumaczenie.
4. **Stop** kończy sesję łagodnie — dokańcza tłumaczenie ostatniej wypowiedzianej frazy
   przed zamknięciem połączenia.

**Pauza/Wznów** działa w obu trybach — wstrzymuje tłumaczenie (zatrzymuje też naliczanie
po stronie Palabry). W trybie **Plik** wznawia dokładnie od tego samego miejsca; w trybie
**Mikrofon** po prostu przestaje wysyłać nowe audio, aż do wznowienia.

W trybie **Plik** dodatkowo:

- **Suwak pozycji** — pokazuje aktualny czas odtwarzania; przeciągnięcie i puszczenie
  przewija tłumaczenie do wskazanego momentu pliku (pomijając to, co było przed/po).

W trybie **Mikrofon** dodatkowo:

- **Głośność mikrofonu** — suwak 0–100%. Przy 0% mikrofon jest wyciszony (wysyła ciszę),
  ale sesja i tak trwa dalej (naliczanie po stronie Palabry nie jest wstrzymane — do tego
  służy Pauza). Przydatne do szybkiego, chwilowego wyciszenia bez przerywania sesji.

**Odśwież urządzenia** — przycisk obok wyboru mikrofonu ponownie skanuje sprzęt audio, więc
mikrofon podłączony *po* uruchomieniu aplikacji też się pojawi na liście (bez restartu apki).
Zachowuje bieżący wybór, jeśli urządzenie nadal istnieje.

Listy wejścia/wyjścia (Windows) pokazują każde urządzenie tylko raz — system audio potrafi
zgłosić to samo fizyczne urządzenie kilka razy (raz na każde API dźwiękowe: MME, DirectSound,
WASAPI, WDM-KS), więc aplikacja pokazuje tylko wersję WASAPI każdego urządzenia.

### Log w głównym oknie

- **Pokaż w logu** — filtr (źródłowy i tłumaczenie / tylko źródłowy / tylko tłumaczenie)
  działa "na żywo": zmiana filtra od razu przefiltrowuje już wyświetlony log, a nie tylko
  kolejne wypowiedzi.
- **Pokaż tagi języka ([pl]/[en])** — checkbox włączający/wyłączający prefiksy językowe
  przy każdej linijce logu.
- **Zapisz transkrypcję...** — zapisuje do pliku `.txt` dokładnie to, co aktualnie widać
  w logu (czyli z uwzględnieniem wybranego filtra i ustawienia tagów).
- **Wyczyść transkrypcję** — jedyny sposób na wyczyszczenie logu, historii i okienka overlay.
  Kliknięcie Start/Stop **nie** czyści ich automatycznie — kolejne sesje w ramach tego samego
  uruchomienia apki doklejają się do tego, co już było, dopóki nie klikniesz tego przycisku.

### Odczepiane okienko z tłumaczeniem (do OBS)

Przycisk **"Odczep okienko z tłumaczeniem"** otwiera osobne, bezramkowe okno pokazujące
na żywo napisy — do przechwycenia w OBS jako Window Capture, niezależnie od głównego okna
aplikacji. Obsługa:

- **Kolejne zdania tej samej wypowiedzi łączą się w jedną linijkę** — nowa linijka zaczyna
  się dopiero po dłuższej przerwie w mówieniu (ok. 2,5 s), traktowanej jako nowa myśl.
- **Najnowsza wypowiedź zawsze jest widoczna** — jeśli tekstu jest więcej, niż mieści się w
  bieżącym rozmiarze okienka (albo zmniejszysz je uchwytem), widok przewija się do najnowszej
  treści, a nie zostaje przy najstarszej.
- **Przeciągnij** dowolne miejsce okienka lewym przyciskiem myszy, żeby je przesunąć.
- **Uchwyt w rogu** (prawy dolny) do zmiany rozmiaru.
- **Pozycja i rozmiar zapamiętują się automatycznie** (po ok. 0,4 s od puszczenia) i wracają
  przy kolejnym otwarciu okienka, także po restarcie aplikacji. Jeśli zapisana pozycja okaże
  się poza ekranem (np. po odłączeniu drugiego monitora), okienko wraca w bezpieczne miejsce
  zamiast zniknąć bez śladu.
- **Ustawienia wyglądu** — dostępne dwoma sposobami: przyciskiem **"Ustawienia wyglądu
  overlay..."** w głównym oknie (zawsze aktywny — jeśli okienko nie jest jeszcze otwarte,
  otworzy się automatycznie) albo prawym klikiem bezpośrednio na okienku. Po otwarciu
  panelu w okienku pojawia się tekst testowy, żeby widzieć efekt zmian na żywo, nawet bez
  trwającej sesji — po zamknięciu panelu ustawień tekst testowy znika (chyba że w
  międzyczasie napłynęło już prawdziwe tłumaczenie — wtedy zostaje ono). **Każda zmiana
  zapisuje się od razu na dysk** (`~/.sts_bridge/overlay_settings.json`)
  i zostaje zapamiętana przy kolejnym uruchomieniu aplikacji. Dostępne:
  - własny filtr (niezależny od głównego okna) — źródłowy i tłumaczenie / tylko źródłowy / tylko tłumaczenie,
  - czcionka i jej rozmiar,
  - kolor tekstu i kolor tła,
  - **cień pod tekstem** — poprawia czytelność, szczególnie ważne przy przezroczystym tle,
  - **nieprzezroczystość tła** — przy 0% tło jest w pełni przezroczyste (naprawdę, na poziomie
    kanału alfa — w OBS zaznacz "Allow Transparency" przy źródle Window Capture), widoczny
    jest tylko tekst.
  - "Zawsze na wierzchu".
- **Prawy klik → Zamknij okienko** (albo ponowne kliknięcie przycisku w głównym oknie) je zamyka.

## Szybkość tłumaczenia

Aplikacja jest skonfigurowana pod niższe opóźnienie: tłumaczy fragmenty zdań na bieżąco, zanim
mówca skończy mówić (zamiast czekać na całe, potwierdzone zdanie), i uznaje zdanie za skończone
po 0,5 s ciszy (zamiast domyślnych 0,7 s). Kompromis: wcześnie pokazane tłumaczenie czasem
zmienia się po usłyszeniu całości zdania, a mówienie z dłuższymi pauzami w środku zdania może
skutkować jego przedwczesnym podzieleniem.

## Znane ograniczenie API

Zweryfikowane testem na żywo: na starcie **każdej nowej sesji** serwer Palabra potrzebuje
ok. 1–1.5 sekundy "rozgrzewki", zanim zacznie pewnie rozpoznawać mowę — pierwsze wypowiedziane
słowo lub dwa tuż po kliknięciu Start mogą nie zostać przetłumaczone. Dodanie ciszy przed
właściwą treścią tego nie naprawia (sprawdzone eksperymentalnie). To jednorazowy koszt na
początku sesji, nie problem powtarzający się przy każdym zdaniu — przy dłuższej rozmowie/pliku
jest pomijalny. Praktyczna rada: zacznij mówić z niewielkim naddatkiem (np. "Dzień dobry,
zaczynamy...") zanim przejdziesz do treści, która ma się liczyć.

## Poza zakresem tej wersji

- Tłumaczenie na wiele języków jednocześnie (jedna sesja = jeden język docelowy na raz).
- Integracja z Zoom (np. jako bot-interpreter na kanale Language Interpretation).
- Miksowanie przetłumaczonego dźwięku z oryginałem.
- Klonowanie głosu mówcy (funkcja dostępna w API Palabra, nieużywana tutaj).
