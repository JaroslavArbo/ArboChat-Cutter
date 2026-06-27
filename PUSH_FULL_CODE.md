# Jak nahrát plnou lokální verzi ArboChat Cutteru

Tento repozitář už obsahuje základní pořádek, `.gitignore`, README a malé konfigurační soubory.

Plný lokální kód nahraj ze svého Macu takto:

```bash
cd /Users/kolarik/test/arbochat_cutter

# Pro jistotu smaž pracovní/generované věci z indexu, kdyby se někdy přidaly
git init
git remote remove origin 2>/dev/null || true
git remote add origin https://github.com/JaroslavArbo/ArboChat-Cutter.git
git branch -M main

# Stáhni aktuální základ z GitHubu
git pull origin main --allow-unrelated-histories

# Přidej jen zdrojáky a malé konfigurační soubory
git add app.py README.md VERSION.txt run_app.command .gitignore
git add frontend/package.json frontend/package-lock.json frontend/index.html frontend/src
git add config/internal_terms.json config/topics_example.csv

# Zkontroluj, že se nepřidávají videa/projekty
git status

# Commit a push
git commit -m "Add ArboChat Cutter v6.6 source"
git push -u origin main
```

Před `git commit` nesmí být ve výpisu `git status` nic jako:

```text
projects/
work/
exports/
*.mp4
*.mov
*.m4a
*.wav
*.zip
node_modules/
web/
```

Pokud se tam něco takového objeví, zastav a uprav `.gitignore` nebo daný soubor odeber z indexu:

```bash
git rm --cached -r projects work exports web frontend/node_modules node_modules 2>/dev/null || true
```
