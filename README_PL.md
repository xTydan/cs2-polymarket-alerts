# CS2 Polymarket Radar v1.5 — aktualizacja do GitHub Actions

Ta paczka aktualizuje **istniejący** radar v1.4. Nie zakładaj nowego repozytorium ani nowego bota Telegram. Działający harmonogram w cron-job.org i dotychczasowy `radar.yml` pozostają bez zmian.

## Instalacja

1. Zrób kopię zapasową aktualnych `radar.py` i `config.json` z repozytorium.
2. Wejdź do `https://github.com/xTydan/cs2-polymarket-alerts` → **Code**.
3. Podmień w **głównym folderze repozytorium** oba pliki `radar.py` i `config.json` zawartością tej paczki. Zatwierdź **Commit changes** do `main`.
4. **Nie przesyłaj** pliku `.env`, tokenu Telegrama, bazy SQLite ani historii z Twojego komputera.
5. Pozostaw `github_state.py`, `.github/workflows/radar.yml`, GitHub Secrets i konfigurację cron-job.org **bez zmian**.
6. Przejdź do **Actions → CS2 Polymarket Radar → Run workflow** i uruchom z `test_telegram = false`. Otwórz zadanie `scan` i sekcję **Skanuj Polymarket lub przetestuj Telegram**.
7. W logach zobaczysz `TX | lower/unknown | ... | $... | portfel ... | ALERT ...` lub `BRAK ALERTU ...`. Dopiero jeśli nowy alert zostanie zakwalifikowany i wysłany, przyjdzie wiadomość na Telegram.
8. Aby nie skanować dwa razy, przy sprawnym cron-job.org można usunąć tylko blok `schedule` z workflow, ale **nie usuwaj** `workflow_dispatch` (potrzebny do wyzwalania przez API).

## Nowe działanie

- Raportowane w logach są **nowe, zaobserwowane** zakupy od 100 USD na znalezionych rynkach CS2, z kwotą, wynikiem, meczem, skrótem portfela i przyczyną alertu lub odrzucenia. Wcześniej zapisane transakcje nie będą powtarzane.
- Niższy tier (rozpoznany po słowach w nazwie rozgrywek): pojedynczy zakup **≥500 USD**, suma zakupów jednego portfela na jeden wynik **≥900 USD w 6 h**, albo **≥1500 USD w 24 h**.
- Nieustalony tier: odpowiednio **≥1000 USD**, **≥1700 USD / 6 h**, **≥3500 USD / 24 h**.
- Kilka portfeli na jeden wynik: co najmniej **3 portfele**, łączna kwota **≥3000 USD w 10 minut**.
- Przy pierwszym skanie po godzinie **21:00 czasu polskiego** bot wysyła codzienny ranking maksymalnie 10 największych zaobserwowanych sum zakupów *jednego portfela na jeden wynik* na rozpoznanych niższych ligach w danym dniu. Jeśli nie ma takich zakupów, raport informuje o braku danych. Powstaje najwyżej jeden raport na dzień (jeśli zapis bazy zostanie przywrócony).
- Poprawiono przetwarzanie stron transakcji w kolejności chronologicznej.
- Stan/wykryte transakcje i kolejka wiadomości pozostają w tej samej bazie SQLite; istniejący `github_state.py` nadal ją przenosi między skanami.

## Istotne ograniczenia

- Nie oznaczamy turnieju jako oficjalnie „Tier 3”; klasyfikacja to heurystyka. Część turniejów i portfeli może zostać pominięta.
- Sumujemy zaobserwowane **zakupy brutto**, a nie aktualny kapitał w pozycji. Nie odejmujemy sprzedaży i nie obejmujemy transakcji niewidocznych dla API lub przed zainstalowaniem monitora.
- Dane na API Polymarket i harmonogram GitHub/cron-job.org mogą mieć opóźnienia. Sygnał nie dowodzi ustawienia meczu ani posiadania informacji poufnych.
- Codzienne raporty na publicznym GitHubie nie stanowią archiwum niezależnego od bazy; jeśli stan zniknie, nie odtworzą się wcześniejsze zakupy.

## Testy

W paczce wdrożeniowej są tylko pliki do podmiany i instrukcja; kod źródłowy był testowany na przykładowych, **fikcyjnych** danych (25 testów offline). Integracja na żywo z Twoim GitHubem i Telegramem nie była w tym wydaniu uruchamiana.
