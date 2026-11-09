# CS2 Polymarket Radar v1.10 — Terminy + filtr obrotu

Aktualizacja do wersji **v1.9**. Wymienia tylko `radar.py`, `build_dashboard.py`, `config.json` w głównym katalogu repozytorium. Nie zmieniaj `.github/workflows/radar.yml`, `github_state.py`, sekretów Telegrama ani cron-job.org. Nie wrzucaj `.env`, lokalnej bazy lub tokenów.

## Co zmienia

- Godzinę meczu odczytuje z `gameStartTime` konkretnego rynku oraz `startTime` wydarzenia. Dodatkowo obsługuje daty ISO z `Z`, przesunięciem strefy, ze spacją lub bez, a także znaczniki Unix w sekundach / milisekundach. Daty bez oznaczonej strefy są interpretowane jako UTC. Na stronie każdą godzinę wyświetla w **Europe/Warsaw**. Nie używa `startDate` (data wystawienia rynku) ani `endDate` (termin zakończenia/rozliczenia) jako godziny rozpoczęcia meczu.
- Wyszukuje łączny **obrót wydarzenia Polymarket** (`volume` / `volumeNum`, a jeśli niepodany — sumę `volumeNum` z rynków). Nie myli go z `liquidity` ani z zakupami zauważonymi przez radar.
- **Nowe wydarzenia ze znanym obrotem mniejszym niż 100 USD są ignorowane przy wyborze rynków do skanowania.** Dotyczy to *całego wydarzenia*, nie pojedynczej transakcji ani zakładu. Gdy obrót wzrośnie do 100 USD, mecz może pojawić się podczas kolejnego discovery (domyślnie maksymalnie ok. 10 minut od poprzedniego discovery; opóźnienie GitHub i API może być większe).
- Kiedy API nie zwraca wiarygodnego obrotu, wydarzenie nie jest odrzucane tylko z tego powodu. Kiedy nie zwraca godziny — panel pokazuje „Bez terminu”, zamiast zgadywać.
- Na stronie obok meczu widać `Obrót Polymarket` osobno od `Radar: ... zakupów` (to tylko zaobserwowane zakupy). Dodano filtr obrotu od 0, 100, 250, 500 lub 1000 USD. Domyślnie 100 USD.
- Stan konta użytkownika, historia SQLite i alerty Telegram pozostają w dotychczasowej postaci. Uaktualniony cache ma nowy podpis i zostanie odświeżony po wdrożeniu.

## Instalacja

1. **Wypakuj** archiwum ZIP na swoim komputerze.
2. Otwórz: https://github.com/xTydan/cs2-polymarket-alerts
3. Wejdź w **Code → Add file → Upload files** i wgraj **trzy pliki**: `radar.py`, `build_dashboard.py`, `config.json` (nadpisz istniejące pliki w **głównym katalogu**).
4. Kliknij **Commit changes** na gałęzi `main`.
5. W **Actions → CS2 Polymarket Radar → Run workflow** wybierz `test_telegram = false` i uruchom normalny skan. Obserwuj log. Powinien pojawić się komunikat podobny do:

   ```text
   Odkryto 200 wydarzeń CS2 i 600 rynków; odfiltrowano 30 wydarzeń z obrotem < $100; brak obrotu w 5; brak terminu w 20 rynkach
   ```

   *Wszystkie liczby wyżej są przykładowe.*
6. Poczekaj na zakończenie etapu publikacji panelu, odśwież GitHub Pages `Ctrl + F5`.

## Ustawienia

W `config.json`:

```json
"minimum_event_volume_usd": 100,
"discover_seconds": 600,
"lookback_seconds": 900
```

`minimum_event_volume_usd = 0` pozwala monitorować wszystkie rynki niezależnie od obrotu. `discover_seconds` oznacza co ile sekund radar ponawia wyszukiwanie wydarzeń; przebiegi GitHub Actions i cron-job.org nadal odbywają się co około 5 minut. `lookback_seconds = 900` zwiększa szansę wychwycenia zakupów w wydarzeniu, które właśnie przekroczyło 100 USD, ale nie gwarantuje pełnej historii podczas dłuższych opóźnień lub awarii.

## Co znaczy „Tier 4”?

Obrót poniżej 100 USD to **Twój filtr popularności**, nie oficjalny tier rozgrywek. Zdarza się, że mecz silniejszych zespołów ma mały obrót przed rozpoczęciem, dlatego świadomie możesz przeoczyć pierwsze zakłady, zanim wydarzenie osiągnie 100 USD. W razie potrzeby zmniejsz próg lub ustaw `0`.

## Znane ograniczenia

- Terminy dostępne na stronie Polymarket nie muszą być dostępne w polach `gameStartTime`/`startTime` publicznej odpowiedzi Gamma dla każdego wydarzenia. Nie próbujemy odgadywać godziny z `endDate`, `startDate` ani daty publikacji; te mecze pozostaną „Bez terminu”.
- Filtr po obrocie stosowany jest **po pobraniu metadanych wydarzeń Gamma**, ale **przed** pobraniem transakcji z Data API. To ogranicza liczbę monitorowanych rynków i koszt skanowania; nie eliminuje konieczności pobrania katalogu wydarzeń.
- W bazie pozostaną wcześniejsze transakcje i metadane, w tym z wydarzeń obecnie odfiltrowanych. Nie usuwamy ich podczas aktualizacji.
- Czas rzeczywistego uruchomienia może różnić się od zaplanowanego (opóźnienia turniejów). Status LIVE nie jest potwierdzany z Polymarket.
- Nie sprawdzono zapytań przeciwko żywemu API po wdrożeniu. Testy lokalne obejmują parsowanie dat, grupowanie eventów, filtr 100 USD, migrację SQLite i działanie panelu w Chromium.
