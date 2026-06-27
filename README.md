# ArboChat Cutter

Lokální aplikace pro přípravu, střih a finalizaci záznamů ArboChat.

Aktuální pracovní větev: **React / ViteCutTimeline**.

## Co patří do repozitáře

Do GitHubu patří pouze zdrojový kód a malé konfigurační soubory:

- `app.py`
- `frontend/`
- `config/internal_terms.json`
- `config/topics_example.csv`
- `README.md`
- `.gitignore`
- `VERSION.txt`

Do GitHubu nepatří pracovní videa, exporty ani projektová data:

- `projects/`
- `work/`
- `exports/`
- `node_modules/`
- `web/`
- `*.mp4`, `*.mov`, `*.m4a`, `*.wav`, `*.zip`

## Spuštění

```bash
cd arbochat_cutter
cd frontend
npm install
npm run build
cd ..
python3 app.py
```

Aplikace poběží na:

```text
http://127.0.0.1:8787
```

## Doporučený workflow

1. Novou verzi nejdřív otestuj lokálně.
2. Do repozitáře commituj jen zdrojový kód.
3. Pracovní složku `projects/` nech vždy mimo Git.
4. Každou funkční verzi označ v `VERSION.txt` a `CHANGELOG.md`.

## Poznámka k médiím

Intro/outro šablony a pracovní nahrávky jsou velké soubory. Do běžného GitHub repozitáře nepatří. Pokud je budeš chtít verzovat, použij Git LFS nebo samostatné úložiště.
