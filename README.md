# Trading Coug (suivi 200 SMA)

Watchlist, graphique et indicateur « 200-Day Simple Moving Average (Signals) » avec alertes push et e-mail.
Installation : voir [GUIDE.md](GUIDE.md).

* `web/` : application (HTML, service worker, manifest, Lightweight Charts 5.2.1 sous licence Apache 2.0)
* `scripts/update.py` : récupération des cours, 200 SMA, signaux, alertes
* `config/symbols.json` : liste de surveillance modifiable
* `tests/test_signals.py` : vérifie le moteur de signal contre une transcription littérale du script Pine
* `.github/workflows/update.yml` : planification, publication GitHub Pages
