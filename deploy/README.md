# Wdrożenie

Ten katalog obsługuje ten sam wariant Docker Compose na WSL i VPS. Docker
zarządza autostartem bazy, API i bota przez `restart: unless-stopped`.
API ma dokładnie jeden worker; na production działa w nim scheduler, a na
development scheduler jest domyślnie wyłączony.

## Nowa instalacja

Wymagane są Docker Engine z Compose v2 oraz OpenSSL. Na VPS potrzebny jest
reverse proxy HTTPS; port API pozostaje publikowany wyłącznie na
`127.0.0.1`. Może to być Tailscale Serve albo publiczny Caddy.

```bash
git clone <adres-repozytorium> health_agent
cd health_agent
./deploy/install.sh production  # VPS
# ./deploy/install.sh            # lokalny development
```

Pierwsze wywołanie tworzy `.env`, zapisuje `APP_ENV` i stan schedulera,
generuje hasło bazy oraz sekret webhooka i zatrzymuje się, aby można było
wpisać tokeny. Po uzupełnieniu pliku uruchom skrypt ponownie; bez argumentu
zachowa tryb zapisany w `.env`. Sekretów nie należy commitować ani wklejać
do logów.

Development i production muszą używać osobnych `TELEGRAM_BOT_TOKEN`, gdy
oba boty mogą działać równocześnie. Ten sam `TELEGRAM_CHAT_ID` jest dozwolony,
ale osobny prywatny czat z botem dev ułatwia rozpoznanie środowiska. Telefon
powinien wysyłać właściwy webhook tylko do production.

Przed pierwszym uruchomieniem po aktualizacji starszej instalacji uzupełnij
brakujące `APP_ENV` i `SCHEDULER_ENABLED`. Lokalnie najpierw zastąp token
production tokenem osobnego bota dev, ustaw `development/false`, a dopiero
potem uruchom Compose. Na VPS ustaw `production/true`. Instalator nie zgaduje
trybu istniejącego `.env`, jeśli brakuje w nim `APP_ENV`.

Dashboard wymaga hasła. Wygeneruj hash po instalacji zależności:

```bash
uv run health-agent hash-password
```

Wpisz wynik do `.env` jako wartość w pojedynczych cudzysłowach, np.
`DASHBOARD_PASSWORD_HASH='$argon2id$...'`. Na publicznym VPS pozostaw
`DASHBOARD_COOKIE_SECURE=true`.

Dla Tailscale można wystawić lokalne API poleceniem wykonywanym na hoście:

```bash
sudo tailscale serve --bg http://127.0.0.1:8000
```

Adres HTTPS ustaw jako bazę webhooka w telefonie. Dashboard jest pod
`https://adres/dash`. Nie wystawiaj portu 8000 bezpośrednio; publiczny ma
być wyłącznie reverse proxy z TLS.

## Istniejąca baza

Instalator celowo nie generuje `POSTGRES_PASSWORD` dla istniejącego
`.env`. Wartość musi odpowiadać hasłu zapisanemu w istniejącym wolumenie.
W dotychczasowej lokalnej konfiguracji domyślną wartością było
`health_agent_dev`; przed migracją na VPS należy ją zmienić kontrolowaną
procedurą, a nie samą edycją Compose.

Eksport starej instancji wykonaj przed przełączeniem procesów. Na czas
przenosin zatrzymaj stare API i bota, aby nie działały dwa schedulery ani
dwa procesy Telegram long polling. Dump można odtworzyć tak:

```bash
gunzip -c backup.sql.gz | docker compose exec -T db psql -U health_agent -d health_agent
```

Najpierw przetestuj odtworzenie na bazie testowej. Nie używaj tego polecenia
na właściwej bazie bez zweryfikowanego backupu.

## Utrzymanie

```bash
./deploy/smoke-test.sh
./deploy/doctor.sh
docker compose logs -f api
docker compose logs -f bot
./deploy/update.sh
```

`smoke-test.sh` tworzy odizolowany PostgreSQL, stosuje migracje, zapisuje
syntetyczny rekord, wykonuje prawdziwy backup i odtwarza go do drugiej bazy.
Po zakończeniu usuwa swoje tymczasowe kontenery, sieć i wolumen.

`update.sh` buduje obraz, wykonuje backup przed migracją, zatrzymuje API i bota,
stosuje migracje, uruchamia usługi i czeka na ich gotowość. Jeśli migracja
się nie powiedzie, procesy pozostają zatrzymane do ręcznej diagnozy. Kod
należy wcześniej pobrać świadomie przez `git pull --ff-only`; skrypt nie
aktualizuje repozytorium automatycznie.

Backupy bazy i archiwa zdjęć z manifestem znajdują się w nazwanym wolumenie
`health_agent_backups`; robocze zdjęcia są w `health_agent_photos`.
Kopia na tym samym hoście nie chroni przed awarią dysku; osobny backup poza
VPS pozostaje wymagany.

## Autostart

Na VPS ustaw `APP_ENV=production` i `SCHEDULER_ENABLED=true` (robi to
`./deploy/install.sh production`) oraz włącz usługę Docker przy starcie:

```bash
sudo systemctl enable --now docker
```

Na Windows/WSL Docker Desktop musi mieć włączone uruchamianie przy logowaniu.
Po uruchomieniu demona Docker kontenery z `restart: unless-stopped` wracają
automatycznie. Jeżeli Docker Desktop nie uruchamia dystrybucji WSL, można
dodać w Harmonogramie zadań Windows komendę
`wsl.exe -d Ubuntu-24.04 -- true`.
Restart procesów aplikacji można sprawdzić bez wyłączania hosta; pełny test
autostartu wymaga jednak restartu Docker Desktop/WSL albo serwera i ponownego
uruchomienia `./deploy/doctor.sh`.
