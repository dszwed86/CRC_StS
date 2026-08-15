# CRC Translator

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

1. Pobierz **`CRC.Translator.exe`** (GitHub zamienia spacje w nazwie pliku na kropki).
2. Kliknij dwa razy. To wszystko — nic więcej nie trzeba instalować.

**macOS**

1. Pobierz **`CRC-Translator-macOS.zip`** i rozpakuj (dwuklik w Finderze).
2. Przy **pierwszym** uruchomieniu macOS zablokuje aplikację ("nie można zweryfikować dewelopera") —
   to jednorazowy krok, macOS tak ostrzega przed każdą aplikacją spoza App Store bez płatnego
   (99$/rok) certyfikatu Apple Developer, którego ten projekt nie ma. Odblokuj jednym z dwóch sposobów:
   - **Terminal (najpewniejsze, jedna komenda)** — otwórz Terminal i wpisz (dostosuj ścieżkę, jeśli
     rozpakowałeś gdzie indziej niż Downloads):
     ```bash
     xattr -cr ~/Downloads/"CRC Translator.app"
     ```
     Potem zwykłe podwójne kliknięcie już działa.
   - **Przez Ustawienia systemowe** — spróbuj otworzyć aplikację (pokaże się blokada), potem
     **System Settings → Privacy & Security**, przewiń w dół do komunikatu o zablokowanej
     aplikacji i kliknij **Open Anyway** ("Otwórz mimo to"), potwierdź hasłem/Touch ID, uruchom
     ponownie.

   (Samo kliknięcie prawym przyciskiem → Otwórz, opisywane jako standardowy sposób obejścia
   Gatekeepera, na nowszych wersjach macOS — Sonoma/Sequoia — czasem już nie wystarcza.)
3. Przy **pierwszym** kliknięciu Start macOS zapyta o dostęp do mikrofonu — kliknij **Zezwól**.
   Znany jednorazowy przypadek: jeśli mimo kliknięcia "Zezwól" tłumaczenie i tak nie słyszy mowy
   (a inne aplikacje w tym momencie też nie widzą mikrofonu, dopóki CRC Translator działa) —
   zamknij aplikację całkowicie i uruchom ją ponownie. To macOS-owa osobliwość przy zupełnie
   pierwszym przyznaniu zgody na mikrofon (strumień otwarty tuż przed decyzją systemu zostaje
   "zawieszony" na czas życia tego konkretnego procesu); każde kolejne uruchomienie już działa
   normalnie od razu, bez tego kroku.

Każda kolejna sesja to już tylko podwójne kliknięcie w pobrany plik — nic się nie instaluje
ani nie pobiera przy starcie.

**Sprawdzanie aktualizacji** — aplikacja przy każdym starcie w tle, jednorazowo, sprawdza
najnowsze wydanie na GitHubie. Jeśli jest nowsza wersja, w lewym górnym rogu pojawia się
klikalny link do niej. Brak internetu albo niedostępny GitHub nie przeszkadza w normalnym
uruchomieniu — sprawdzenie po prostu się nie udaje po cichu, bez żadnego komunikatu błędu.

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

**Ustawienia zapamiętują się między uruchomieniami** — wybrany mikrofon/wyjście, głośność
i próg czułości mikrofonu, języki, głos, "Tylko napisy", filtr i tagi w logu zapisują się do
`~/.sts_bridge/app_settings.json` przy zamknięciu aplikacji i wracają przy następnym starcie.
Ustawienie, które wskazuje na już niepodłączone urządzenie albo nieistniejący język, jest po
prostu pomijane (reszta wraca normalnie) zamiast powodować błąd.

Przy pierwszym uruchomieniu kliknij **Ustawienia...** i wklej klucz API Palabra —
zostanie zapisany lokalnie w `~/.sts_bridge/.env`. W tym samym oknie:

- **Testuj klucz** — realnie sprawdza połączenie z API (bez uruchamiania płatnej sesji
  tłumaczenia) i pokazuje, czy klucz działa, czy jest odrzucany.
- **Otwórz panel Palabra (saldo, użycie)** — otwiera w przeglądarce
  `platform.palabra.ai/api-keys`, gdzie widoczne jest saldo kredytów i historia użycia.
  Palabra nie udostępnia tego w publicznym API, więc apka nie może pokazać salda
  bezpośrednio — to najbliższe dostępne rozwiązanie.
- **Saldo Palabra (USD, orientacyjne)** — opcjonalne pole: wpisz tu saldo sprawdzone w panelu
  Palabry (wyżej), a aplikacja będzie je na bieżąco pomniejszać o szacowany koszt każdej
  sesji (licznik obok Start pokaże "saldo ~$X.XX"). To tylko **przybliżenie liczone lokalnie**,
  nie prawdziwe saldo — może się rozjechać z rzeczywistością (np. jeśli ten sam klucz API jest
  używany też gdzie indziej, albo aplikacja zamknie się w trakcie sesji bez czystego Stop).
  Wróć tu od czasu do czasu i wpisz aktualną wartość z panelu Palabry, żeby zsynchronizować.
  Puste pole przy zapisie zostawia zapisane saldo bez zmian (nie zeruje go).
- **Historia sesji...** — lista zakończonych sesji (data, czas trwania, orientacyjny koszt)
  zapisywana lokalnie w `~/.sts_bridge/session_history.json`, z sumą na dole. Jedyny zapis
  przeszłego zużycia dostępny w samej aplikacji, skoro Palabra nie udostępnia historii przez
  API. Przycisk **Wyczyść historię** w tym oknie usuwa cały zapis (nieodwracalnie).

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

1. Wybierz mikrofon z listy — jest zawsze aktywny w trakcie sesji, niezależnie od tego, czy
   dodasz też plik.
2. Opcjonalnie dodaj plik audio/wideo do zmiksowania z mikrofonem, przyciskiem **"Wybierz
   plik..."** w sekcji "Plik (opcjonalnie)". Uszkodzony, pusty lub nieobsługiwany plik jest
   wykrywany od razu przy wyborze (czytelny komunikat), zamiast dopiero przy kliknięciu Start.
   Przycisk **✕** obok usuwa wybrany plik. Oba działania — wybór i usunięcie pliku — działają
   też **w trakcie trwającej sesji**, nie tylko przed Start; zobacz szczegóły niżej.
3. Wybierz **Wyjście (do OBS)** — wirtualny kabel podpięty pod OBS (patrz sekcja Konfiguracja
   OBS wyżej). Przycisk **"Testuj wyjście"** obok odtwarza krótki dźwięk testowy na wybrane
   urządzenie, bez uruchamiania żadnej sesji Palabra (czyli bez kosztu) — pozwala od razu
   sprawdzić, czy OBS faktycznie odbiera dźwięk z tego urządzenia, zanim zaczniesz płatną
   sesję. W trakcie trwającej sesji pasek **Poziom wyjścia** (obok pola Wyjście) pokazuje na
   żywo, że przetłumaczony dźwięk faktycznie dociera do wybranego urządzenia — nie tylko że
   został odebrany od serwera.
4. Ustaw język źródłowy i docelowy (domyślnie polski → angielski) oraz opcjonalnie **Głos**:
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

   **Tylko napisy (bez dźwięku)** — checkbox obok wyboru wyjścia: przetłumaczone audio nie jest
   odtwarzane na wybrane urządzenie, zostaje tylko tekst (log/okienko z napisami). Palabra nie
   oferuje trybu bez syntezy mowy, więc koszt sesji się nie zmienia — to tylko wycisza
   odtwarzanie po stronie aplikacji, przydatne gdy zależy Ci wyłącznie na napisach do OBS.
   **Można to przełączać także w trakcie trwającej sesji**, nie tylko przed Start — zaznaczenie
   natychmiast wycisza dźwięk (łącznie z tym, co akurat czeka w kolejce odtwarzania), a
   odznaczenie natychmiast je z powrotem włącza.
5. Kliknij **Start**. W dolnym panelu pojawia się na żywo transkrypcja i tłumaczenie. Jeśli
   masz wybrany plik, nie zaczyna się on odtwarzać automatycznie — patrz niżej.
6. **Stop** kończy sesję łagodnie — dokańcza tłumaczenie ostatniej wypowiedzianej frazy
   przed zamknięciem połączenia.

**Skróty klawiszowe** — **F5** to to samo co kliknięcie Start/Stop, **F6** to samo co Pauza.
Działają niezależnie od tego, który element interfejsu jest aktualnie aktywny.

**Czas trwania i orientacyjny koszt sesji** — licznik obok statusu pokazuje czas, przez który
sesja faktycznie nalicza u Palabry (czyli bez czasu spędzonego w Pauzie), oraz przybliżony
koszt wg stawki $0.04/min z dokumentacji Palabra. To wyliczenie po stronie aplikacji, nie
prawdziwe saldo — Palabra nie udostępnia salda przez API (patrz **Otwórz panel Palabra**
w Ustawieniach, żeby zobaczyć rzeczywiste zużycie).

**Dwa niezależne przyciski pauzy:**

- **Pauza/Wznów** — zawsze wstrzymuje/wznawia **całą sesję** (zatrzymuje też naliczanie po
  stronie Palabry), niezależnie od tego, czy masz wybrany plik. Mikrofon po prostu przestaje
  wysyłać nowe audio, aż do wznowienia. Jeśli masz wybrany plik, ten czas jest z niego
  **pomijany** (w przeciwieństwie do Pauzy pliku niżej, ta pauza nie zapamiętuje miejsca) —
  po Wznów plik gra dalej od aktualnej, "przewiniętej" w czasie pozycji, a nie od miejsca
  sprzed Pauzy.
- **Pauza pliku/Wznów plik** — osobny przycisk, widoczny tylko gdy masz wybrany plik.
  Wstrzymuje/wznawia **wyłącznie odtwarzanie pliku**, lokalnie — mikrofon zostaje aktywny i
  wciąż tłumaczony (naliczanie u Palabry trwa dalej) przez cały ten czas. Wznawia dokładnie
  od tego samego miejsca w pliku.

**Plik — zawsze widoczny, opcjonalny, można zmieniać w trakcie sesji:**

- **Nowo wybrany/zmieniony plik zawsze zaczyna wstrzymany** — czy to wybrany przed kliknięciem
  Start, czy dodany/zmieniony w trakcie trwającej sesji, plik nigdy nie zaczyna grać sam;
  trzeba kliknąć **Pauza pliku/Wznów plik**, żeby go uruchomić.
- **Dodawanie, zmiana i usuwanie pliku działają też w trakcie trwającej sesji** — przycisk
  **"Wybierz plik..."** w dowolnym momencie podmienia (lub dodaje) plik bez przerywania ani
  restartowania sesji; **✕** usuwa go, a mikrofon leci dalej bez przerwy. To czysto lokalna
  zmiana źródła audio (serwer Palabra nie wie, że plik się zmienił) — bez ryzyka błędów API.
- **Suwak pozycji** — pokazuje aktualny czas odtwarzania pliku; przeciągnięcie i puszczenie
  przewija plik do wskazanego momentu (pomijając to, co było przed/po). Dotyczy wyłącznie
  ścieżki pliku, mikrofon nie jest tym przewijaniem w żaden sposób dotknięty. Dekodowanie
  pliku zaczyna się natychmiast po jego wybraniu/dodaniu (w tle, niezależnie od stanu pauzy
  i od tego, czy sesja zdążyła się już połączyć z serwerem) — dzięki temu suwak staje się
  aktywny, a "Wznów plik" realnie odtwarza dźwięk, zauważalnie szybciej niż gdyby dekodowanie
  czekało na oba te kroki.
- **Gdy plik się skończy, sesja leci dalej na samym mikrofonie** — nie zatrzymuje się
  automatycznie; trzeba kliknąć Stop ręcznie.

**Mikrofon — zawsze aktywny w trakcie sesji, niezależnie od pliku:**

- **Zmiana mikrofonu w trakcie sesji** — lista wyboru mikrofonu (inaczej niż reszta ustawień)
  zostaje aktywna także po kliknięciu Start. Wybór innego urządzenia z listy przełącza na nie
  natychmiast, bez przerywania ani restartowania sesji — to czysto lokalna zmiana (serwer
  Palabra nigdy nie wie, z jakiego fizycznego mikrofonu pochodzi dźwięk), więc nie ma ryzyka
  błędów po stronie API, jakie występowały przy próbie zmiany głosu w locie.
- **Głośność mikrofonu** — suwak 0–100%. Przy 0% mikrofon jest wyciszony (wysyła ciszę),
  ale sesja i tak trwa dalej (naliczanie po stronie Palabry nie jest wstrzymane — do tego
  służy Pauza). Przydatne do szybkiego, chwilowego wyciszenia bez przerywania sesji.
- **Poziom sygnału** — pasek pod suwakiem Głośności, aktywny tylko w trakcie trwającej sesji,
  pokazuje na żywo, że mikrofon faktycznie odbiera dźwięk. Pokazuje surowy poziom wejściowy
  (sprzed Głośności/Ignoruj ciszej niż), więc reaguje niezależnie od tych ustawień — przydatne
  do szybkiego sprawdzenia, czy wybrane urządzenie w ogóle coś łapie.
- **Ignoruj ciszej niż** — suwak 0–100%, domyślnie wyłączony (0%). Inaczej niż głośność (która
  ścisza wszystko po równo, także Twój głos), to prawdziwy próg: dźwięk cichszy niż ustawiony
  poziom jest całkowicie wycinany (zamieniany na ciszę) *zanim* zostanie wysłany, a głośniejszy
  (bliższa mowa) przechodzi bez zmian. **Uwaga na kierunek** — im wyżej ustawisz suwak, tym
  WIĘCEJ dźwięku jest odcinane (nie odwrotnie); zbyt wysoka wartość odetnie też Twoją własną,
  cichszą mowę. Pomaga odciąć ciche, odległe dźwięki — w tym własne tłumaczenie dobiegające
  z głośnika, jeśli mikrofon może je usłyszeć — zacznij od niskiej wartości (15–20%) i
  zwiększaj tylko w razie potrzeby.

**Odśwież urządzenia** — przycisk obok wyboru mikrofonu ponownie skanuje sprzęt audio, więc
mikrofon podłączony *po* uruchomieniu aplikacji też się pojawi na liście (bez restartu apki).
Zachowuje bieżący wybór, jeśli urządzenie nadal istnieje.

Listy wejścia/wyjścia (Windows) pokazują każde urządzenie tylko raz — system audio potrafi
zgłosić to samo fizyczne urządzenie kilka razy (raz na każde API dźwiękowe: MME, DirectSound,
WASAPI, WDM-KS), więc aplikacja pokazuje tylko wersję WASAPI każdego urządzenia.

### Log w głównym oknie

- **Powtarzające się linie zwijają się automatycznie** — gdy to samo (finalne) zdanie/słowo
  pojawi się kilka razy pod rząd (np. "Alleluja" powtórzone 5 razy), log pokazuje jedną linię
  z licznikiem ("Alleluja x5") zamiast pięciu identycznych linii. Dotyczy zarówno głównego
  logu, jak i odczepianego okienka z napisami — każde ma własny licznik. Źródło i tłumaczenie
  liczone są niezależnie.
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
- **Błędy zapisują się też do pliku** `~/.sts_bridge/errors.log` (z znacznikiem czasu), oprócz
  pokazania się w logu głównego okna — nigdy w okienku z napisami. Przydatne, żeby odtworzyć
  błąd sprzed zamknięcia aplikacji albo taki, który wyszedł z widocznego zakresu logu.
  Przycisk **"Otwórz log błędów"** otwiera ten plik od razu w domyślnym edytorze tekstu.

### Odczepiane okienko z tłumaczeniem (do OBS)

Przycisk **"Odczep okienko z tłumaczeniem"** otwiera osobne, bezramkowe okno pokazujące
na żywo napisy — do przechwycenia w OBS jako Window Capture, niezależnie od głównego okna
aplikacji. Obsługa:

- **Znaczek z czasem sesji** (prawy górny róg) — to samo co licznik czasu/kosztu w głównym
  oknie, widoczne bez przełączania się z powrotem do apki podczas transmisji. Widoczny tylko
  w trakcie trwającej sesji — znika, gdy sesja się kończy.
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

## Odporność na zerwane połączenie

Jeśli połączenie WebSocket z Palabrą padnie w trakcie sesji (np. krótka przerwa w sieci), apka
**sama próbuje połączyć się ponownie** — do 3 prób, z rosnącym odstępem (2s/5s/10s). Status
pokazuje wtedy "Rozłączono, ponawiam próbę...". Mikrofon i plik działają cały czas normalnie
w tle — to, co zostało powiedziane w trakcie samej przerwy, nie zostanie przetłumaczone (nie ma
buforowania na zapas), ale po udanym ponownym połączeniu tłumaczenie od razu wraca do bieżącego
momentu, zamiast doganiać zaległość. Połączenie, które utrzyma się przynajmniej 10 sekund, zanim
znowu padnie, odnawia pulę 3 prób od nowa — ale kilka prób z rzędu, gdzie serwer wpuszcza i od
razu znów rozłącza, **nie** resetują puli w kółko, więc apka faktycznie się kiedyś podda zamiast
próbować bez końca. Jeśli wszystkie próby zawiodą (albo błąd nie nadaje się do ponawiania, np.
zły klucz API), sesja kończy się zwykłym Błędem jak dotychczas — trzeba kliknąć Start ręcznie.

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

## Długie sesje / pliki (2h+)

Sprawdzone testem: tryb Plik radzi sobie z plikami 2-godzinnymi bez problemu. Dekodowanie
całego pliku na raz zajmuje ok. 330 MB pamięci na 2h dźwięku (jednorazowo, w pełni zwalniane
po zakończeniu sesji) — bez wycieków pamięci w trakcie samej sesji: historia transkrypcji jest
ograniczona do ostatnich 300 zdarzeń, a okienko z napisami zawsze pokazuje tylko kilka
ostatnich linijek, niezależnie od długości sesji. Jedyna rzecz, która rośnie z czasem, to
widoczna zawartość głównego logu w oknie aplikacji (celowo nieograniczona — zobacz "Wyczyść
transkrypcję" wyżej) — dla pełnej 2-godzinnej sesji to rzędu kilkuset KB tekstu, nieistotne
dla pamięci komputera.

## Poza zakresem tej wersji

- Tłumaczenie na wiele języków jednocześnie (jedna sesja = jeden język docelowy na raz).
- Integracja z Zoom (np. jako bot-interpreter na kanale Language Interpretation).
- Miksowanie przetłumaczonego dźwięku z oryginałem.
