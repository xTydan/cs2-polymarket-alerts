# CS2 Polymarket Radar — GitHub Actions + Telegram

Wersja uruchamiana co ~5 minut w GitHub Actions, bez włączonego PC. Korzysta z publicznego Polymarket Gamma API i Data API oraz istniejącego bota Telegram. **Kod nie służy do automatycznego zawierania zakładów.**

## Dlaczego repozytorium PUBLIC?

GitHub Actions w publicznym repozytorium wykorzystującym standardowe hostowane maszyny jest bezpłatny w ramach aktualnych zasad. W prywatnym repozytorium GitHub Free przewiduje 2000 minut/miesiąc, co przy odpalaniu co 5 minut może nie wystarczyć. Kod jest publiczny, ale token Telegram musi być przechowywany wyłącznie w `Settings > Secrets and variables > Actions` (nigdy w plikach repozytorium!). Artefakty z bazą historii publicznych transakcji mogą być widoczne dla osób z dostępem do repozytorium.

## Instalacja bez terminala

1. Zaloguj się na [github.com](https://github.com), kliknij `+` > **New repository**.
2. Nazwa: np. `cs2-polymarket-alerts`; zaznacz **Public** i utwórz repozytorium.
3. Kliknij **Add file > Upload files** i wgraj `radar.py`, `github_state.py`, `requirements.txt`, `config.json`, `README_PL.md`, `.gitignore` oraz katalog `tests` (opcjonalny). **Nie wgrywaj żadnego `.env`** z Windowsa.
4. Wejdź w **Add file > Create new file**, w polu nazwy wpisz dokładnie `.github/workflows/radar.yml`, wklej treść z pobranej paczki z tego pliku, następnie **Commit changes**. Przy wgrywaniu przez GitHub Desktop można skopiować cały katalog paczki wraz z `.github` i zrobić jeden commit.
5. Wejdź w **Settings > Secrets and variables > Actions > New repository secret**. Dodaj osobno:
   - `TELEGRAM_BOT_TOKEN` = token już istniejącego bota od BotFather.
   - `TELEGRAM_CHAT_ID` = identyfikator rozmowy Telegram z wcześniejszego pliku `.env`.
   Nie wpisuj cudzysłowów. Nie przesyłaj nikomu tokenu i nie publikuj `.env`.
6. Wejdź w **Actions > CS2 Polymarket Radar > Run workflow**, zaznacz `test_telegram` na **true**. Po zakończeniu uruchomienia powinieneś dostać wiadomość testową na telefonie.
7. W **Actions > Run workflow** uruchom ponownie z `test_telegram` na **false**. Pierwszy skan tworzy stan bazowy i nie wysyła starych alertów; kolejne zadania uruchamiają się automatycznie około co 5 minut.
8. Wyłącz lokalną wersję bota na Windowsie (Ctrl+C), żeby nie było podwójnych alertów.

### Powiadomienia na telefonie

Zainstaluj Telegram na telefonie, zaloguj się na swoje konto, otwórz rozmowę z botem, naciśnij `Start`, a następnie włącz powiadomienia dla tej rozmowy. Nie trzeba mieć uruchomionego PC ani otwartej przeglądarki.

## Jak działa monitor

- Odkrywa aktywne mecze CS2 przez Gamma API i pobiera transakcje przez Data API v2.
- Wykrywa pojedyncze duże zakupy, zakupy dużego udziału względem wolumenu, serie jednego portfela i klastry różnych portfeli obstawiających jeden wynik.
- Odfiltrowuje rozpoznane duże turnieje. Poziom Tier 2/3 jest oceniany heurystycznie, a nie potwierdzany oficjalną klasyfikacją.
- Przy pierwszym uruchomieniu nie wysyła powiadomień o dawnych transakcjach.
- Po każdym skanie zapisuje bazę i cache jako skompresowany artefakt GitHub Actions z czasem przechowania 1 dzień. Dzięki temu po następnych uruchomieniach nie duplikuje zwykłych alertów. Jeżeli przez ponad 24 h nie będzie udanego przebiegu, stan może wygasnąć.
- Jeżeli zadanie się opóźni, odzyskuje do jednej godziny zaległych zakupów, jeżeli są jeszcze dostępne w stronicowaniu API. Nie gwarantuje to pełnego pokrycia w okresie awarii.

## Progi

Domyślny minimalny pojedynczy zakup: 1000 USD, bezwzględnie duży zakup: 5000 USD, zakup ≥25% wolumenu, seria jednego portfela ≥2500 USD w 5 min, kilka portfeli ≥6000 USD w 5 min. Zmień progi w `config.json`. Obserwacja co 5 minut oznacza opóźnienie i możliwe przeoczenia na bardzo płynnych rynkach.

## Ograniczenia

- GitHub może **opóźniać lub pomijać** zadania cron. `schedule` w publicznym repozytorium może zostać automatycznie wyłączone po 60 dniach bez aktywności.
- Pierwsze znalezienie wydarzeń bywa długie. Repozytorium publiczne udostępnia kod i może ujawniać zanonimizowaną/pseudonimizowaną historię transakcji w artefaktach, ale token i ID czatu są trzymane w sekcji Secrets.
- API Polymarket może ograniczać żądania albo zmieniać schemat. Limit `max_events` może spowodować niepełne wykrycie wydarzeń; bot zapisuje ostrzeżenie w logach Actions.
- Dane o transakcjach nie dowodzą ustawiania meczu. Bot nie zawiera zakładów i nie jest wskazówką inwestycyjną.
- Przewiduj, że artefakty są usuwane po 24h, gdy zadanie długo nie działa. Gdy pojawi się ponownie, zacznie od nowa, nie wysyłając starych transakcji.

## Diagnose błędu

Otwórz **Actions**, kliknij ostatni przebieg, następnie `scan`. W kroku skanowania zobaczysz np. `Odkryto ... wydarzeń` albo błąd API. Jeśli test Telegram nie przyjdzie, sprawdź oba sekrety i czy rozpocząłeś rozmowę z botem.

Oficjalne źródła:
- https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- https://github.com/pricing
- https://data-api.polymarket.com/v2/docs
